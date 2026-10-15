"""data_export: part2

Documented in: docs/architecture/data-export.md
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from sqlalchemy import and_, false, or_, true
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    ApiKey,
    AuditLog,
    Connection,
    ConnectorIdMap,
    ContentType,
    ContextProfile,
    Customer,
    CustomFieldDef,
    DatabricksExportRun,
    DataExportRun,
    DataExportStatus,
    DeliveryEmptyApproval,
    DepartmentBudget,
    Document,
    EngineProfile,
    EvalCandidate,
    EvalDataset,
    EvalDatasetItem,
    EvalRun,
    EvalRunItem,
    EvalScore,
    FullTextTM,
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
    GlossaryTerm,
    KeyChangeLog,
    LanguageSet,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemoryEvidence,
    MemorySuggestion,
    MemorySuggestionEvidence,
    Organization,
    Project,
    ProjectBudget,
    ProjectFieldValue,
    ProjectGlossary,
    ProjectMember,
    ProjectPairProvider,
    ProjectTemplate,
    ProjectTmSource,
    PromptTemplate,
    QueryPost,
    QueryThread,
    RoutingRule,
    Segment,
    SegmentationProfile,
    SegmentEmbedding,
    SegmentPostEdit,
    SegmentTM,
    StepRun,
    StringKey,
    StyleGuide,
    SyncLog,
    TapAction,
    TapRun,
    TargetLocale,
    TermCandidate,
    User,
    UserGroup,
    UserGroupMember,
    Vendor,
    VendorCustomer,
    VendorDispatch,
    WebhookEvent,
    Workflow,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access
from app.services.data_export_part1 import (
    _REAP_STATUS_KEY,
    MANIFEST_SCHEMA,
    ExportRefused,
    Extractor,
    ResolvedScope,
    _app_version,
    _applies,
    _documents_subq,
    _flat_cell,
    _has_live_run,
    _members_digest,
    _org_val,
    _proj_membership_leg,
    _reap_status_mac,
    _resolve_scope,
    _run_uuid_from_tmp,
    _tap_action_clause,
)


def _cust_clause(model, scope, *, global_bearing):
    base = model.customer_id.in_(scope.customers)
    o = _org_val(scope)
    if global_bearing and scope.include_org_shared and o is not None and hasattr(model, "org_id"):
        base = or_(base, model.org_id == o)
    return base


def _cust_or_proj_clause(model, scope, *, global_bearing):
    # #371: project-bearing rows must pass the project boundary. Customer/org grants
    # apply only to shared rows; children reuse this predicate through their parent subquery.
    # The project leg excludes NULL even for an unrestricted operator (TA-03/DX-309).
    base = or_(
        and_(model.project_id.is_(None), model.customer_id.in_(scope.customers)),
        _proj_membership_leg(model.project_id, scope),
    )
    o = _org_val(scope)
    if global_bearing and scope.include_org_shared and o is not None and hasattr(model, "org_id"):
        base = or_(base, and_(model.project_id.is_(None), model.org_id == o))
    return base


def _org_clause(model, scope):
    # #73 FAIL-CLOSED (TA-02): a BLOCKED principal matches NOTHING (never the unfiltered "all").
    if scope.org == access.BLOCKED_ORG_ID:
        return false()
    o = _org_val(scope)
    # org None (genuine single-tenant/operator, never a real multi-org user, LAW11) -> all rows.
    return true() if o is None else (model.org_id == o)


def _segments_subq(scope):
    return select(Segment.id).where(Segment.document_id.in_(_documents_subq(scope)))


def _workflow_runs_subq(scope):
    return select(WorkflowRun.id).where(WorkflowRun.document_id.in_(_documents_subq(scope)))


def _tap_actions_subq(scope):
    return select(TapAction.id).where(_tap_action_clause(scope))


def _eval_dataset_clause(scope):
    return and_(
        EvalDataset.customer_id.in_(scope.customers),
        or_(
            EvalDataset.source_document_id.is_(None),
            EvalDataset.source_document_id.in_(_documents_subq(scope)),
        ),
    )


def applicable_extractors(scope, history, options) -> list:
    """The extractors that WILL emit a member for (scope, history, options) -- the member
    whitelist test [E]/[P] recomputes exactly this."""
    return [ex for ex in ENTITY_REGISTRY if _applies(ex, scope, history, options)]


def build_manifest(run_uuid, scope, history, counts, dropped, members, audit_slice=None) -> dict:
    # ``audit_slice`` defaults to the not-included shape so a self/customer/project artifact (and
    # any workspace export without the admin audit toggle) reports honestly. Train C's
    # ``_write_audit_slice`` passes the real {included, rows, chain_verify} for a workspace dump.
    return {
        "manifest_schema": MANIFEST_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "app_version": _app_version(),
        "run_uuid": run_uuid,
        "scope": {
            "kind": scope.scope_kind,
            "ref": scope.scope_ref,
            "org_id": _org_val(scope),
            "customer_id": scope.clamp,
        },
        "history": history,
        "counts": counts,
        "dropped": dropped,
        "members": members,
        "audit_slice": audit_slice or {"included": False, "rows": 0, "chain_verify": None},
        "artifact_sha256": _members_digest(members),
    }


def _resolve_scope_from_row(session, row: DataExportRun) -> ResolvedScope:
    """RE-DERIVE the confinement SERVER-SIDE from the PERSISTED DataExportRun row (TA-01, a spec
    LAW): the row was written under the RBAC path (run_export / submit_export), whereas the job
    payload is caller-supplied and forgeable (POST /api/jobs/run hands an arbitrary payload to
    HANDLERS["data_export"] with no scope re-validation). So the handler NEVER trusts the payload
    as the ACL -- it re-runs _resolve_scope from the row's (scope_kind, scope_ref, org_id, actor,
    trusted_operator) and recomputes projects/customers/include_identity itself.

    Principal: ``trusted_operator`` rows re-resolve as the in-process operator (user=None,
    confined by the stored org_id); every other row re-resolves under the row's actor looked up as
    a User (a per-user key's actor IS its owner email, deps.api_identity) -- FAIL-CLOSED if that
    principal no longer resolves. A ``workspace`` row skips the admin re-check (require_admin off):
    the submit-time _is_workspace_admin already gated its creation and the admin-scope signal is
    not persisted, so confinement is re-derived from the stored org ring alone."""
    if row.trusted_operator:
        user, org_param = None, row.org_id
    else:
        user = (
            session.exec(select(User).where(User.email == row.actor)).first() if row.actor else None
        )
        # #483: an erased / deactivated principal must not run a queued export (fail-closed).
        if user is None or not getattr(user, "active", True):
            raise ExportRefused(
                "data export principal no longer resolvable; refusing (fail-closed)."
            )
        org_param = None  # a real user's org is derived from the user (current_org_id)
    return _resolve_scope(
        session,
        user=user,
        ident=None,
        org_id=org_param,
        scope_kind=row.scope_kind,
        scope_ref=row.scope_ref,
        require_admin=(row.scope_kind != "workspace"),
    )


def _new_run_row(
    scope,
    run_uuid,
    history,
    emitters,
    ia,
    include_eval,
    include_embeddings,
    actor,
    *,
    trusted_operator,
) -> None:
    """Persist the RBAC-resolved run row. The confinement DECISION lives HERE (scope_kind/
    scope_ref/org_id/customer_id/actor + trusted_operator + the option columns), so the handler
    re-derives the export from this row, never the forgeable job payload (TA-01)."""
    with Session(engine) as s:
        s.add(
            DataExportRun(
                run_uuid=run_uuid,
                org_id=_org_val(scope),
                customer_id=scope.clamp,
                actor=actor,
                scope_kind=scope.scope_kind,
                scope_ref=scope.scope_ref,
                history=history,
                fmt="jsonl",
                emitters=list(emitters),
                include_audit=ia,
                include_eval=bool(include_eval),
                include_embeddings=bool(include_embeddings),
                trusted_operator=bool(trusted_operator),
                status=DataExportStatus.QUEUED,
            )
        )
        s.commit()


def _build_payload(scope, run_uuid, opts_hash, actor) -> dict:
    """The job payload carries ONLY the run_uuid (to find the row), the opts_hash (submit_unique
    dedupe key), and the scope TUPLE (the _assert_payload_matches_row fast-fail). It deliberately
    does NOT carry projects/customers/include_* -- those are re-derived server-side from the row
    (TA-01), so a forged payload cannot widen the walk."""
    return {
        "run_uuid": run_uuid,
        "scope_kind": scope.scope_kind,
        "scope_ref": scope.scope_ref,
        "actor": actor,
        "org": _org_val(scope),
        "customer_id": scope.clamp,
        "opts_hash": opts_hash,
    }


ENTITY_REGISTRY: tuple = (
    Extractor("organization", Organization, "self_org", "self_org", ws_only=True),
    Extractor(
        "customer",
        Customer,
        "own_customer",
        "own_customer",
        scrub_cols=frozenset({"name", "settings", "active_pairs", "default_provider"}),
    ),
    Extractor("content_type", ContentType, "customer_col", "own_customer", global_bearing=True),
    Extractor("project", Project, "own_project", "own_customer"),
    Extractor("target_locale", TargetLocale, "project_col", "parent_fk", parent_col="project_id"),
    Extractor(
        "project_pair_provider",
        ProjectPairProvider,
        "project_col",
        "parent_fk",
        parent_col="project_id",
    ),
    Extractor(
        "project_tm_source", ProjectTmSource, "project_col", "parent_fk", parent_col="project_id"
    ),
    Extractor(
        "project_glossary", ProjectGlossary, "project_col", "parent_fk", parent_col="project_id"
    ),
    Extractor(
        "delivery_empty_approval",
        DeliveryEmptyApproval,
        "project_col",
        "parent_fk",
        parent_col="project_id",
    ),
    Extractor(
        "document",
        Document,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        scrub_cols=frozenset({"skeleton", "context"}),
    ),
    Extractor(
        "segment",
        Segment,
        "doc_col",
        "parent_fk",
        parent_col="document_id",
        scrub_cols=frozenset({"source_text", "target_text", "meta"}),
    ),
    Extractor("string_key", StringKey, "project_col", "parent_fk", parent_col="project_id"),
    Extractor(
        "key_change_log",
        KeyChangeLog,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        history="full",
        scrub_cols=frozenset({"actor"}),
    ),
    Extractor(
        "segment_post_edit",
        SegmentPostEdit,
        "doc_col",
        "parent_fk",
        parent_col="document_id",
        history="full",
        scrub_cols=frozenset({"baseline_text"}),
    ),
    Extractor(
        "step_run",
        StepRun,
        "steprun",
        "parent_fk",
        parent_col="workflow_run_id",
        history="full",
        scrub_cols=frozenset({"input_text", "output_text", "error"}),
    ),
    Extractor(
        "memory_event",
        MemoryEvent,
        "memory_customer",
        "own_customer",
        history="full",
        scrub_cols=frozenset({"before_text", "after_text", "note", "actor"}),
    ),
    Extractor(
        "sync_log",
        SyncLog,
        "connection_col",
        "parent_fk",
        parent_col="connection_id",
        history="full",
        scrub_cols=frozenset({"message"}),
    ),
    Extractor(
        "webhook_event",
        WebhookEvent,
        "connection_col",
        "parent_fk",
        parent_col="connection_id",
        history="full",
    ),
    Extractor(
        "tap_action",
        TapAction,
        "tap_action",
        "provenance_int",
        cursor_type=str,
        provenance_col="project_id",
        history="full",
        scrub_cols=frozenset({"manifest", "totals"}),
    ),
    Extractor(
        "tap_run",
        TapRun,
        "tap_run",
        "parent_fk",
        parent_col="action_id",
        cursor_type=str,
        history="full",
        scrub_cols=frozenset({"detail"}),
    ),
    Extractor(
        "databricks_export_run",
        DatabricksExportRun,
        "org_col",
        "own_org",
        ws_only=True,
        history="full",
    ),
    Extractor(
        "segment_tm",
        SegmentTM,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        global_bearing=True,
        tm_not_null=True,
        # Retention-only lifecycle metadata is not part of the existing export contract.
        drop_cols=frozenset({"activity_at"}),
        scrub_cols=frozenset({"source_text", "target_text", "meta"}),
    ),
    Extractor(
        "full_text_tm",
        FullTextTM,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        global_bearing=True,
        tm_not_null=True,
        drop_cols=frozenset({"activity_at"}),
        scrub_cols=frozenset({"source_text", "target_text"}),
    ),
    Extractor(
        "segment_embedding",
        SegmentEmbedding,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        global_bearing=True,
        tm_not_null=True,
        history="full",
    ),
    Extractor("glossary", Glossary, "customer_col", "own_customer", global_bearing=True),
    Extractor("glossary_pair", GlossaryPair, "glossary_col", "parent_fk", parent_col="glossary_id"),
    Extractor(
        "glossary_entry",
        GlossaryEntry,
        "glossary_col",
        "parent_fk",
        parent_col="glossary_id",
        scrub_cols=frozenset({"source_term", "target_term", "meta"}),
    ),
    Extractor(
        "glossary_sense",
        GlossarySense,
        "entry_col",
        "parent_fk",
        parent_col="glossary_entry_id",
        scrub_cols=frozenset({"target_term", "context_cue"}),
    ),
    Extractor(
        "glossary_concept",
        GlossaryConcept,
        "glossary_col",
        "parent_fk",
        parent_col="glossary_id",
        scrub_cols=frozenset({"definition", "notes"}),
    ),
    Extractor(
        "glossary_term",
        GlossaryTerm,
        "concept_col",
        "parent_fk",
        parent_col="concept_id",
        scrub_cols=frozenset({"term", "usage_note", "context_example"}),
    ),
    Extractor(
        "term_candidate",
        TermCandidate,
        "glossary_col",
        "parent_fk",
        parent_col="glossary_id",
        history="full",
        scrub_cols=frozenset({"source_term", "proposed_target", "context_snippet"}),
    ),
    Extractor(
        "style_guide",
        StyleGuide,
        "customer_col",
        "own_customer",
        global_bearing=True,
        scrub_cols=frozenset({"body"}),
    ),
    Extractor(
        "segmentation_profile",
        SegmentationProfile,
        "customer_col",
        "own_customer",
        global_bearing=True,
    ),
    Extractor(
        "context_profile",
        ContextProfile,
        "cust_or_proj",
        "own_customer",
        global_bearing=True,
        scrub_cols=frozenset({"context", "name"}),
    ),
    Extractor("language_set", LanguageSet, "org_col", "own_org", ws_only=True),
    Extractor(
        "lqa_result", LQAResult, "lqa", "parent_fk", parent_col="document_id", current_filter="lqa"
    ),
    Extractor("workflow_group", WorkflowGroup, "customer_col", "own_customer", global_bearing=True),
    Extractor("workflow", Workflow, "cust_or_proj", "own_customer", global_bearing=True),
    Extractor("workflow_step", WorkflowStep, "workflow_col", "parent_fk", parent_col="workflow_id"),
    Extractor("workflow_batch", WorkflowBatch, "wf_or_doc", "parent_fk", parent_col="workflow_id"),
    Extractor(
        "workflow_run",
        WorkflowRun,
        "doc_col",
        "parent_fk",
        parent_col="document_id",
        scrub_cols=frozenset({"context", "notes"}),
    ),
    Extractor(
        "prompt_template",
        PromptTemplate,
        "customer_col",
        "own_customer",
        global_bearing=True,
        scrub_cols=frozenset({"body"}),
    ),
    Extractor(
        "engine_profile",
        EngineProfile,
        "org_col",
        "own_org",
        ws_only=True,
        drop_cols=frozenset({"api_key_enc"}),
        nested_drop={"config": ["access_key_secret_enc"]},
        url_cols=frozenset({"base_url", "config"}),  # #217: legacy user:password@ stripped
    ),
    Extractor(
        "project_template",
        ProjectTemplate,
        "customer_col",
        "own_customer",
        global_bearing=True,
        scrub_cols=frozenset({"description"}),
    ),
    Extractor("routing_rule", RoutingRule, "cust_or_proj", "own_customer", global_bearing=True),
    Extractor(
        "query_thread",
        QueryThread,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        scrub_cols=frozenset({"subject"}),
    ),
    Extractor(
        "query_post",
        QueryPost,
        "thread_col",
        "parent_fk",
        parent_col="thread_id",
        history="full",
        scrub_cols=frozenset({"body"}),
    ),
    Extractor(
        "memory_entry",
        MemoryEntry,
        "memory_customer",
        "own_customer",
        current_filter="memory",
        scrub_cols=frozenset({"body", "source_term", "target_term"}),
    ),
    Extractor(
        "memory_evidence",
        MemoryEvidence,
        "memory_col",
        "parent_fk",
        parent_col="memory_id",
        history="full",
    ),
    Extractor(
        "memory_suggestion",
        MemorySuggestion,
        "memory_customer",
        "own_customer",
        scrub_cols=frozenset(
            {"proposed_change", "applied_snapshot", "diff_text", "rationale", "reject_reason"}
        ),
    ),
    Extractor(
        "memory_suggestion_evidence",
        MemorySuggestionEvidence,
        "suggestion_col",
        "parent_fk",
        parent_col="suggestion_id",
        history="full",
    ),
    Extractor(
        "vendor",
        Vendor,
        "vendor",
        "own_org",
        drop_cols=frozenset({"secret_enc"}),
        scrub_cols=frozenset({"last_message"}),
        url_cols=frozenset({"endpoint_url"}),  # #217: legacy URL userinfo stripped
    ),
    Extractor("vendor_customer", VendorCustomer, "customer_col", "own_customer"),
    Extractor(
        "vendor_dispatch",
        VendorDispatch,
        "cust_or_doc",
        "own_customer",
        drop_cols=frozenset({"claim_token"}),
        scrub_cols=frozenset({"rework_feedback", "last_error"}),
    ),
    Extractor("department_budget", DepartmentBudget, "org_col", "own_org", ws_only=True),
    Extractor("project_budget", ProjectBudget, "project_col", "parent_fk", parent_col="project_id"),
    Extractor(
        "custom_field_def", CustomFieldDef, "customer_col", "own_customer", global_bearing=True
    ),
    Extractor(
        "project_field_value",
        ProjectFieldValue,
        "project_col",
        "parent_fk",
        parent_col="project_id",
        scrub_cols=frozenset({"value_text", "value_json"}),
    ),
    Extractor(
        "eval_dataset",
        EvalDataset,
        "eval_dataset",
        "own_customer",
        requires_option="include_eval",
        provenance_col="source_document_id",
        scrub_cols=frozenset({"name", "description"}),
    ),
    Extractor(
        "eval_dataset_item",
        EvalDatasetItem,
        "eval_dataset_child",
        "parent_fk",
        parent_col="dataset_id",
        requires_option="include_eval",
        scrub_cols=frozenset({"source_text", "reference_text", "meta"}),
    ),
    Extractor(
        "eval_candidate",
        EvalCandidate,
        "customer_col",
        "own_customer",
        requires_option="include_eval",
        scrub_cols=frozenset({"name", "description", "config"}),
    ),
    Extractor(
        "eval_run",
        EvalRun,
        "eval_run",
        "own_customer",
        requires_option="include_eval",
        history="full",
    ),
    Extractor(
        "eval_run_item",
        EvalRunItem,
        "eval_run_child",
        "parent_fk",
        parent_col="run_id",
        requires_option="include_eval",
        history="full",
        scrub_cols=frozenset({"output_text", "error"}),
    ),
    Extractor(
        "eval_score",
        EvalScore,
        "eval_run_child",
        "parent_fk",
        parent_col="run_id",
        requires_option="include_eval",
        history="full",
        scrub_cols=frozenset({"detail"}),
    ),
    Extractor(
        "user",
        User,
        "org_col",
        "own_org",
        ws_only=True,
        drop_cols=frozenset({"password_hash", "password_changed_at", "password_must_change"}),
    ),
    Extractor("user_group", UserGroup, "org_col", "own_org", ws_only=True),
    Extractor("user_group_member", UserGroupMember, "user_org", "user_org", ws_only=True),
    Extractor(
        "api_key", ApiKey, "user_org", "user_org", ws_only=True, drop_cols=frozenset({"key_hash"})
    ),
    Extractor(
        "project_member",
        ProjectMember,
        "project_member",
        "parent_fk",
        parent_col="project_id",
        ws_only=True,
    ),
    Extractor(
        "audit", AuditLog, "org_col", "actor_set", ws_only=True, walkable=False, history="full"
    ),
    Extractor(
        "connection",
        Connection,
        "connection_ws",
        "own_org",
        ws_only=True,
        drop_cols=frozenset({"secret_enc"}),
        scrub_cols=frozenset({"last_message"}),
        url_cols=frozenset({"config"}),  # #217: a URL in config never carries userinfo
    ),
    Extractor(
        "connector_id_map",
        ConnectorIdMap,
        "connection_col",
        "parent_fk",
        parent_col="connection_id",
        ws_only=True,
    ),
)


EXPLICIT_EXCLUSIONS: dict = {
    "job": "DX-203: org resolves from an unbounded caller-supplied payload/result JSON; a "
    "shape-scrub is not a redaction guarantee. Run/build history is delivered first-class by "
    "step_run + workflow_run, so a per-kind projection is not worth a half-redaction. Excluded.",
    "jobqueue": "DB dispatch ledger: payload mirrors Job.payload (excluded), job_id a bare "
    "non-FK int. Pure infrastructure, no user content.",
    "invite": "DX-304: no user_id/org_id column, so no clean confinement path; token is a live "
    "secret; a pending invite is transient identity-provisioning state, not portable content.",
    "dataexportrun": "the export's OWN audit rows -- self-scope excludes (would recurse); a full "
    "workspace export MAY include them as data_export_run.jsonl (Train C), like databricks.",
    "keyimage": "image metadata + raw pixel blobs are delivered by the KEYS export's "
    "include_string_images ZIP bundle (Batch 14 Phase 1.5, built), NOT by this compliance "
    "dump. This exclusion is PERMANENT and must not lift: rows AND bytes here would strip Fernet "
    "+ escape the size cap, and the per-project operator ZIP already covers the use case.",
    "advancedmission": "Advanced Workflows mission config (harness operational record, opt_in "
    "feature); the LLM-localization-harness's own rows, not core customer content. A later train "
    "may project them into a workspace dump like data_export_run. Excluded until then.",
    "advancedrun": "Advanced Workflows run state (harness operational record) -- status, spend "
    "actuals, correlation ids. The harness's own audit rows (the dataexportrun precedent), not "
    "portable customer content. Excluded.",
    "advancedtranscriptentry": "Advanced Workflows durable transcript (harness operational "
    "record) -- correlated to the provider-call ledger for audit, not core customer content. "
    "Excluded.",
    "advancedstagedtarget": "Advanced Workflows STAGED (uncommitted) candidate targets. A "
    "delivered target is WRITTEN THROUGH quality.commit_mt_output into the classic content table "
    "(Segment / StringKey), which the export already carries; these run-scoped staged rows are the "
    "pre-delivery candidates (a keys/segment value, or -- post-T3 -- whole paragraphs of a "
    "FULL_TEXT document body), not a second copy of the portable delivered content. Excluded.",
    "qualitylens": "Quality Lens configuration (weights, bar, checks) for the Advanced tier -- "
    "the harness's own scoring configuration, the advancedmission precedent. Not customer "
    "content; excluded with the rest of the Advanced-tier operational graph.",
    "qualitylensexample": "Curated few-shot SPAN examples attached to a lens; lens configuration "
    "rather than delivered content, and it follows its parent lens. Excluded.",
    "qualityfinding": "Quality Lens findings (span + severity) the Advanced judge + checks write "
    "against a staged target. Parented to an advancedrun (an excluded operational row), so "
    "exporting it would dangle; it follows the Advanced tier, which is excluded. The customer's "
    "portable quality record stays lqa_result (classic tier, parented to exported documents).",
    "qualitygolditem": "Meta-evaluation GOLD data (an immutable labelled hypothesis) for the "
    "Evaluations Lab. Lab measurement input authored by the install's own labellers, not "
    "customer content; the Lab's isolation rule keeps it out of production reporting. Excluded.",
    "qualitygoldlabel": "One human span label on a gold item; follows its parent gold item. "
    "Excluded.",
    "userlayout": "Train L1: a person's saved card arrangement for a web page type (a UI "
    "preference, owned by one account or the auth-off 'local' owner). "
    "Not customer content and meaningless outside the web UI. Excluded.",
    "loginthrottle": "Operational password lockout and admission quotas, not customer content.",
    "usermfa": (
        "TOTP train: an account's two-step sign-in authenticator (a Fernet-encrypted shared "
        "secret plus the replay step). Credential material, like password_hash; never "
        "exported. Excluded."
    ),
    "mcpoauthgrant": (
        "#753: an account's consented MCP OAuth grants (client, redirect URI, scopes, bound "
        "resource) carrying SHA-256 digests of live codes and tokens. Credential material, like "
        "api_key's key_hash; the account's own grants appear in its data-subject access export "
        "without the digests. Excluded."
    ),
    "notifychannel": (
        "Project automation Part B: an org's outbound notification channels (Slack, Teams, signed "
        "webhook, email). The destination URL and signing secret are a Fernet blob, i.e. "
        "credential material, never exported. Excluded."
    ),
    "notifyroute": (
        "Project automation Part B: which notification channel a scope (project, content type, "
        "project type, customer, org) posts to. Administrative routing configuration, not customer "
        "content; it follows its channel, which is excluded."
    ),
    "notifyoutbox": (
        "Project automation Part B: the notification outbox and delivery log (event, status, "
        "attempts, error class). Operational record carrying names and counts only, never content; "
        "follows its channel. Excluded."
    ),
    "mfarecoverycode": (
        "TOTP train: Argon2id hashes of an account's single-use two-step sign-in recovery "
        "codes. Credential material, never exported. Excluded."
    ),
    "passwordhistory": (  # pregate: allow-secret table name, not a credential
        "Password policy: Argon2id hashes of an account's previous local "
        "passwords, kept only to refuse reuse. Credential material (the password_hash drop_cols "
        "precedent on the user table), never exported. Excluded."
    ),
    "systempromptoverride": (
        "#356 phase 2a: an org admin's versioned override of two pipeline system prompt heads. "
        "Org-wide administrative configuration authored by staff, not customer content or a "
        "person's personal data (it carries no document text). Excluded."
    ),
    "workflowsystempromptoverride": (
        "#356 phase 2b: a workflow's own versioned override of two pipeline system prompt heads "
        "(the per-workflow twin of systempromptoverride). Administrative workflow "
        "configuration authored by staff, not customer content or a person's personal data "
        "(it carries no document text). Excluded."
    ),
    "assistallowance": (
        "F25 #301: per-identity Ask/Draft call counters keyed by a keyed HMAC of the caller "
        "(no email, no content), rolled over each minute/day and removed by retention and "
        "erasure. Operational rate-limit state, not customer content. Excluded."
    ),
    "projectautomation": (
        "Project automation (MVP part A): a project's run schedule, pause state and per-run "
        "cap, plus the connection it pulls from. Operational configuration authored by staff, "
        "not customer content; the connection it names is classified on its own. Excluded."
    ),
    "projectbindingclaim": (
        "Project automation: which project owns the write-back of one external item when a "
        "connection feeds several projects. An external id plus a project id, no content. "
        "Operational state, like connectorIdMap's sibling bookkeeping. Excluded."
    ),
    "blobdeleteoutbox": (
        "#399 (finding dbdata-07): the blob-delete retry ledger -- a storage key plus an "
        "attempt count, kept precisely so the bytes it names CAN be erased. It carries no "
        "content and no tenancy column (the document it names is already deleted), so the "
        "reaper's own bookkeeping must not become an export surface. Operational state, "
        "like assistallowance/loginthrottle. Excluded."
    ),
}


INSTALL_GLOBAL_EXCLUDED: dict = {
    "setting": "DX-03: a global install-wide k/v store (SAML/IdP/connector/SMTP config); a "
    "per-org admin may not request it. Install-admin sub-scope only (host CLI), then every "
    "is_secret_key value masked.",
    "spendledger": "PK=period, no org/customer column (DX-03): tenancy-free, un-confinable to a "
    "caller's org -- install-admin only.",
}


# ======================================================================================
# THE ENTITY REGISTRY (LAW13) -- ordered, mirrors databricks_export.STREAMS. Every
# table=True model is classified here OR in EXPLICIT_EXCLUSIONS OR INSTALL_GLOBAL_EXCLUDED;
# the [P] metadata-diff test fails on any unclassified table.
# ======================================================================================
# --- §4.1 core content (current) ---
# Batch 15 T2: the empty-delivery approval record. Anchored on project_id (NOT the now-
# NULLABLE document_id -- a key-mode row has document_id NULL, which a doc_col
# `document_id.in_(subq)` strategy would never match, P3). project_id is NOT NULL in
# BOTH modes -- the target_locale / project_tm_source precedent.
# --- §4.2 history / temporal (full-only) ---
# --- §4.3 language assets ---
# SPEC-terminology-backend T1: the concept/term rich store beneath the compat termbase.
# Customer terminology -- scoped by glossary (concept) / concept (term), exported with the
# customer and scrubbed of its linguistic surfaces in a scrubbed export (LAW13).
# --- §4.4 quality + workflow + collaboration ---
# --- §4.5 memory (LLMemory) ---
# --- §4.6 vendors + cost ---
# --- §4.7 Evaluations Lab (include_eval default OFF; copy-in provenance cross-check) ---
# --- §4.8 identity / security (WS-only, admin scope) ---
# AuditLog: classified here for LAW13 completeness, but WALKED SPECIALLY (via
# audit.iter_export, raw, chain-verified) by Train C's audit_slice() -- NOT the generic
# walker. walkable=False keeps Train A from emitting it (self-serve never; workspace slice
# is Train C).
# EXPLICIT_EXCLUSIONS + INSTALL_GLOBAL_EXCLUDED (§4.9/§4.10) -- table_name -> reason (LAW13).
# SPEC-advanced-workflows P0: the LLM-localization-harness operational records -- mission
# configs, run state, the durable transcript, and staged (uncommitted) candidate targets.
# They are the harness's OWN operational/audit rows (the dataexportrun precedent), not core
# portable customer content; a full-workspace projection is a later train's call. Excluded.
# SPEC-quality-lenses-p2 T3 (2026-09-02, hand-off (d)): the T1 "T3 revisit" markers are now
# RESOLVED. T3's `quality.judge` DOES write QualityFinding rows against delivered content, so
# the deferred question ("are they portable customer content?") is live -- and the answer is
# NO, all five tables STAY excluded, for a firm reason rather than a deferral:
#   * The Advanced tier's whole operational graph (advancedmission / advancedrun /
#     advancedstagedtarget / advancedtranscriptentry, all directly above) is excluded from the
#     per-customer workspace dump. A QualityFinding is parented (exactly-one-parent) to an
#     advancedrun in this tier -- so exporting findings while excluding their run parents would
#     emit a DANGLING graph, a run_id no exported row defines. The finding follows its parent.
#   * The classic-tier quality record REMAINS exportable as `lqa_result` (parented to exported
#     documents/segments), so a customer's quality data is not lost -- it is the classic tier's
#     lakehouse card, not this tier's span findings, that carries it. P2b (the classic lens
#     backport, a later train that DOES touch documents/segments) is where a lens-scored classic
#     step would decide whether to ALSO emit portable findings; the Advanced tier does not.
#   * Lens CONFIG (qualitylens/qualitylensexample) is scoring configuration, the advancedmission
#     precedent; gold data (qualitygolditem/qualitygoldlabel) is Lab measurement input under the
#     Lab isolation rule. Neither is customer content.
# The set of table names this module classifies (used by the [P] completeness test). The org
# TABLE is classified in ENTITY_REGISTRY (the organization extractor emits only the caller's own
# row); the full org LIST being install-admin-only is a ROW rule, not a table-level entry (DX-312).
def classified_tables() -> set:
    names = {ex.model.__tablename__ for ex in ENTITY_REGISTRY}
    names |= set(EXPLICIT_EXCLUSIONS)
    names |= set(INSTALL_GLOBAL_EXCLUDED)
    return names


def _csv_cell(v):
    from app.output_safety import csv_safe_cell

    if v is None:
        return ""
    return csv_safe_cell(_flat_cell(v))


# --------------------------------------------------------------------------------------
# §8.2 The admin audit slice (D4/LAW5) -- workspace + admin only, emitted RAW (LAW3 carve-out
# DX-08), chain-verified so a downstream store can re-verify the tamper-evident chain.
# --------------------------------------------------------------------------------------
def _write_audit_slice(zf, scope: ResolvedScope, members: list) -> dict:
    """Append `audit.jsonl` for a workspace export: `audit.iter_export` yields SIEM-ready dicts
    (each carrying `prev_hash`/`row_hash`) confined to the caller-org's actors, emitted RAW
    (LAW3 carve-out DX-08 -- scrubbing would break `row_hash` re-verification, the slice's whole
    point). A `verify_chain()` result is stamped into the manifest. Returns the `audit_slice`
    manifest block."""
    from app.services import audit

    org = _org_val(scope)
    hasher = hashlib.sha256()
    nbytes = 0
    nrows = 0
    stream_err = None
    with zf.open("audit.jsonl", "w") as zh:
        try:
            for rec in audit.iter_export(org_id=org, customer_id=None, batch=500):
                b = (
                    json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                ).encode("utf-8")
                zh.write(b)
                hasher.update(b)
                nbytes += len(b)
                nrows += 1
        except Exception as exc:
            stream_err = exc
    # #433: the ZIP entry exists once opened, so list it even when the stream failed midway;
    # the caller records the slice as failed (partial) and the manifest never hides the member.
    members.append(
        {
            "path": "audit.jsonl",
            "kind": "jsonl",
            "bytes": nbytes,
            "sha256": hasher.hexdigest(),
            "truncated": stream_err is not None,
        }
    )
    if stream_err is not None:
        raise stream_err
    ok, checked, first_bad_id = audit.verify_chain()
    return {
        "included": True,
        "rows": nrows,
        "chain_verify": {"ok": bool(ok), "checked": int(checked), "first_bad_id": first_bad_id},
    }


def _read_reap_status():
    """The persisted last-sweep stamp: a parsed dict, None (never run), or "invalid" (present but
    forged / bad-MAC). MAC-verified exactly like audit._read_chain_status."""
    from app.models import Setting

    with Session(engine) as s:
        row = s.get(Setting, _REAP_STATUS_KEY)
    raw = row.value if row else ""
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except (ValueError, TypeError):
        return "invalid"
    if not isinstance(outer, dict):
        return "invalid"
    body, mac = outer.get("v"), outer.get("mac")
    if not isinstance(body, str) or not isinstance(mac, str):
        return "invalid"
    if not hmac.compare_digest(mac, _reap_status_mac(body)):
        return "invalid"
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "invalid"
    return data if isinstance(data, dict) else "invalid"


def _write_reap_status(data: dict) -> None:
    from app.models import Setting

    body = json.dumps(data, separators=(",", ":"), sort_keys=True)
    value = json.dumps(
        {"v": body, "mac": _reap_status_mac(body)}, separators=(",", ":"), sort_keys=True
    )
    with Session(engine) as s:
        row = s.get(Setting, _REAP_STATUS_KEY)
        if row:
            row.value = value
        else:
            row = Setting(key=_REAP_STATUS_KEY, value=value)
        s.add(row)
        s.commit()


def _reap_tmp_orphans(now: datetime, ceiling_delta) -> int:
    """TA-05/TA-07: a HARD kill between put_stream's temp-write and rename (or the `_generate`
    spool) strands a plaintext `dataexport_*.tmp` in EXPORTS_DIR that delete-by-artifact-key never
    touches. Past the grace ceiling, unlink any such orphan whose embedded run_uuid (when present)
    has no live `running`/`queued` run -- so a killed run cannot leave plaintext scoped content
    behind indefinitely. Best-effort (same False-on-OSError posture as the port's delete)."""
    d = config.EXPORTS_DIR
    if not d.exists():
        return 0
    removed = 0
    cutoff = now - ceiling_delta
    for entry in list(d.iterdir()):
        name = entry.name
        if not name.endswith(".tmp") or "dataexport_" not in name:
            continue
        try:
            mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
        except OSError:
            continue
        if mtime >= cutoff:
            continue  # younger than one grace ceiling -- may be an in-flight generation's spool
        run_uuid = _run_uuid_from_tmp(name)
        if run_uuid is not None and _has_live_run(run_uuid):
            continue  # a live run owns this temp -- never unlink under an active generation
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed
