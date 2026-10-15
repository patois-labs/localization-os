"""Comprehensive full-object-graph data export -- the RBAC-confined graph walker, the
entity registry, the JSONL emitter, the manifest, and the export-job core (Batch 13,
Train A; SPEC-data-export §6).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, MANIFEST_SCHEMA, BATCH, NotFound, ExportRefused,
ResolvedScope, _Options, Extractor, _org_val, _is_workspace_admin, _resolve_scope, _proj_clause,
_proj_membership_leg, _documents_subq, _query_threads_subq, _memory_cust_clause, _memory_subq,
_suggestions_subq,
_tap_action_clause, _vendorcustomer_subq, _current_narrow, _scrub_json, _jsonify, _row_to_dict,
_keyset_batches, _applies, JsonlEmitter, _JSONL, _app_version, _members_digest, _enabled,
_s3_export_configured, _check_refuse_gates, _ensure_exports_dir, _file_chunks, _artifact_key,
_audit, _assert_payload_matches_row, _opts_hash, _flat_cell, _sha256_and_size, _REAP_STATUS_KEY,
_reap_status_mac, _parse_iso_naive, _sweep_due, _retention_hours, _grace_minutes,
_fail_dead_running, _run_uuid_from_tmp, _has_live_run, _unlink_run_tmp, _row_covers, _cust_clause,
_cust_or_proj_clause, _org_clause, _segments_subq, _workflow_runs_subq, _tap_actions_subq,
_eval_dataset_clause, applicable_extractors, build_manifest, _resolve_scope_from_row, _new_run_row,
_build_payload, ENTITY_REGISTRY, EXPLICIT_EXCLUSIONS, INSTALL_GLOBAL_EXCLUDED, classified_tables,
_csv_cell, _write_audit_slice, _read_reap_status, _write_reap_status, _reap_tmp_orphans,
_glossaries_subq, _workflows_subq, _connections_subq, _org_users_subq, _eval_datasets_subq,
run_export, submit_export, reap_scheduled_sweep, _reap_once, purge_artifacts_covering,
mark_covering_purged, finish_purge, artifact_servable from
app.services.data_export_part1, app.services.data_export_part2, app.services.data_export_part3.

ONE walker, pluggable emitters (LAW4). ``ENTITY_REGISTRY`` is an ordered tuple of
``Extractor`` records (mirrors ``databricks_export.STREAMS``); ``_iter_entity`` is the single
keyset-paginated, scope-confined, redaction-applying row source; ``walk`` chains it over the
registry as a flat ``(entity, dict)`` stream; ``_generate`` streams one JSONL member per
applicable entity into a spooled ZIP and stages it through the ``ExportStore`` port. There is
exactly one scope predicate and one row projection -- so /api, web and CLI (Train B) all call
this same walker and render a byte-identical artifact.

CONFINEMENT (LAW1/LAW2/LAW11, the HIGH bar): ``_resolve_scope`` builds a ``ResolvedScope`` from
``reports.allowed_project_ids`` (the project-scoping ring) + the ``#73`` org predicate + the
customer clamp + the DX-05 project-derived ``visible_customer_ids``. Every extractor's predicate
is POSITIVE-membership (``col IN scope_set``), which excludes NULL/global rows by construction; a
project-only extractor ALSO ANDs ``project_id IS NOT NULL`` (DX-309). An empty allow-list yields
a shaped-empty artifact, never an unscoped dump.

Train A is host-CLI-only + synchronous (driven through ``jobs.run_sync``). /api + web + the
remote CLI are Train B; CSV/XLSX emitters, the audit slice, S3 + signed-URL delivery, the reaper
and the GDPR-erase hook are Train C. ``ExportStore`` staging + ``DiskExportStore`` ship in Train A
(``app/ports/exportstore.py``) so this is end-to-end testable.

Documented in: docs/architecture/data-export.md
"""

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import uuid
import zipfile
from base64 import b64encode
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, false, or_, true
from sqlmodel import Session, select

from app import config
from app import output_csv as csv
from app.db import engine
from app.models import (
    ApiKey,
    AuditLog,
    Connection,
    ConnectorIdMap,
    ContentType,
    ContextProfile,
    Customer,
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
    MemoryStatus,
    MemorySuggestion,
    MemorySuggestionEvidence,
    Organization,
    Project,
    ProjectBudget,
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
from app.services import access, reports, secrets_scrub, settings, tm_module
from app.services.data_export_part1 import (
    _JSONL,
    _REAP_STATUS_KEY,
    BATCH,
    MANIFEST_SCHEMA,
    ExportRefused,
    Extractor,
    JsonlEmitter,
    NotFound,
    ResolvedScope,
    _app_version,
    _applies,
    _artifact_key,
    _assert_payload_matches_row,
    _audit,
    _check_refuse_gates,
    _current_narrow,
    _documents_subq,
    _enabled,
    _ensure_exports_dir,
    _fail_dead_running,
    _file_chunks,
    _flat_cell,
    _grace_minutes,
    _has_live_run,
    _is_workspace_admin,
    _jsonify,
    _keyset_batches,
    _members_digest,
    _memory_cust_clause,
    _memory_events_subq,
    _memory_subq,
    _Options,
    _opts_hash,
    _org_val,
    _parse_iso_naive,
    _proj_clause,
    _proj_membership_leg,
    _query_threads_subq,
    _reap_status_mac,
    _resolve_scope,
    _retention_hours,
    _row_covers,
    _row_to_dict,
    _run_uuid_from_tmp,
    _s3_export_configured,
    _scrub_json,
    _sha256_and_size,
    _suggestion_evidence_clause,
    _suggestions_subq,
    _sweep_due,
    _tap_action_clause,
    _unlink_run_tmp,
    _vendorcustomer_subq,
)
from app.services.data_export_part2 import (
    ENTITY_REGISTRY,
    EXPLICIT_EXCLUSIONS,
    INSTALL_GLOBAL_EXCLUDED,
    _build_payload,
    _csv_cell,
    _cust_clause,
    _cust_or_proj_clause,
    _eval_dataset_clause,
    _new_run_row,
    _org_clause,
    _read_reap_status,
    _reap_tmp_orphans,
    _resolve_scope_from_row,
    _segments_subq,
    _tap_actions_subq,
    _workflow_runs_subq,
    _write_audit_slice,
    _write_reap_status,
    applicable_extractors,
    build_manifest,
    classified_tables,
)
from app.services.data_export_part3 import (
    _connections_subq,
    _eval_datasets_subq,
    _global_workflows_subq,
    _glossaries_subq,
    _org_users_subq,
    _reap_once,
    _workflows_subq,
    artifact_servable,
    finish_purge,
    mark_covering_purged,
    purge_artifacts_covering,
    reap_scheduled_sweep,
    run_export,
    submit_export,
)
from app.services.maintenance import admitted_work

__all__ = [
    "BATCH",
    "ENTITY_REGISTRY",
    "EXPLICIT_EXCLUSIONS",
    "INSTALL_GLOBAL_EXCLUDED",
    "MANIFEST_SCHEMA",
    "_JSONL",
    "_REAP_STATUS_KEY",
    "ApiKey",
    "AuditLog",
    "Connection",
    "ConnectorIdMap",
    "ContentType",
    "ContextProfile",
    "Customer",
    "DatabricksExportRun",
    "DeliveryEmptyApproval",
    "DepartmentBudget",
    "Document",
    "EngineProfile",
    "EvalCandidate",
    "EvalDataset",
    "EvalDatasetItem",
    "EvalRunItem",
    "EvalScore",
    "ExportRefused",
    "Extractor",
    "FullTextTM",
    "Glossary",
    "GlossaryPair",
    "GlossarySense",
    "GlossaryTerm",
    "JsonlEmitter",
    "KeyChangeLog",
    "LQAResult",
    "LanguageSet",
    "MemoryEntry",
    "MemoryEvent",
    "MemoryEvidence",
    "MemoryStatus",
    "MemorySuggestion",
    "MemorySuggestionEvidence",
    "NotFound",
    "Organization",
    "Project",
    "ProjectBudget",
    "ProjectGlossary",
    "ProjectMember",
    "ProjectPairProvider",
    "ProjectTemplate",
    "ProjectTmSource",
    "PromptTemplate",
    "QueryPost",
    "QueryThread",
    "ResolvedScope",
    "RoutingRule",
    "Segment",
    "SegmentPostEdit",
    "SegmentTM",
    "SegmentationProfile",
    "StepRun",
    "StringKey",
    "StyleGuide",
    "SyncLog",
    "TapAction",
    "TapRun",
    "TargetLocale",
    "TermCandidate",
    "User",
    "UserGroup",
    "UserGroupMember",
    "Vendor",
    "VendorCustomer",
    "VendorDispatch",
    "WebhookEvent",
    "Workflow",
    "WorkflowBatch",
    "WorkflowGroup",
    "WorkflowRun",
    "WorkflowStep",
    "_Options",
    "_app_version",
    "_applies",
    "_artifact_key",
    "_assert_payload_matches_row",
    "_audit",
    "_build_payload",
    "_check_refuse_gates",
    "_connections_subq",
    "_csv_cell",
    "_current_narrow",
    "_cust_clause",
    "_cust_or_proj_clause",
    "_documents_subq",
    "_enabled",
    "_ensure_exports_dir",
    "_eval_dataset_clause",
    "_eval_datasets_subq",
    "_fail_dead_running",
    "_file_chunks",
    "_flat_cell",
    "_glossaries_subq",
    "_grace_minutes",
    "_has_live_run",
    "_is_workspace_admin",
    "_jsonify",
    "_keyset_batches",
    "_members_digest",
    "_memory_cust_clause",
    "_memory_subq",
    "_new_run_row",
    "_opts_hash",
    "_org_clause",
    "_org_users_subq",
    "_org_val",
    "_parse_iso_naive",
    "_proj_clause",
    "_proj_membership_leg",
    "_query_threads_subq",
    "_read_reap_status",
    "_reap_once",
    "_reap_status_mac",
    "_reap_tmp_orphans",
    "_resolve_scope",
    "_resolve_scope_from_row",
    "_retention_hours",
    "_row_covers",
    "_row_to_dict",
    "_run_uuid_from_tmp",
    "_s3_export_configured",
    "_scrub_json",
    "_segments_subq",
    "_sha256_and_size",
    "_suggestions_subq",
    "_sweep_due",
    "_tap_action_clause",
    "_tap_actions_subq",
    "_unlink_run_tmp",
    "_vendorcustomer_subq",
    "_workflow_runs_subq",
    "_workflows_subq",
    "_write_audit_slice",
    "_write_reap_status",
    "applicable_extractors",
    "artifact_servable",
    "b64encode",
    "build_manifest",
    "classified_tables",
    "dataclass",
    "field",
    "finish_purge",
    "hmac",
    "mark_covering_purged",
    "purge_artifacts_covering",
    "reap_scheduled_sweep",
    "reports",
    "run_export",
    "submit_export",
    "timezone",
    "tm_module",
    "uuid",
]

log = logging.getLogger("loc_tms.data_export")


def _glossary_entries_subq(scope):
    return select(GlossaryEntry.id).where(GlossaryEntry.glossary_id.in_(_glossaries_subq(scope)))


def _glossary_concepts_subq(scope):
    return select(GlossaryConcept.id).where(
        GlossaryConcept.glossary_id.in_(_glossaries_subq(scope))
    )


def _eval_run_clause(scope):
    return and_(
        EvalRun.customer_id.in_(scope.customers),
        EvalRun.dataset_id.in_(_eval_datasets_subq(scope)),
    )


def _eval_runs_subq(scope):
    return select(EvalRun.id).where(_eval_run_clause(scope))


def _scope_clause(ex: Extractor, scope: ResolvedScope):
    """The POSITIVE-membership WHERE for one extractor (LAW1/LAW11). Dispatch on strategy."""
    m = ex.model
    s = ex.strategy
    if s == "self_org":
        if scope.org == access.BLOCKED_ORG_ID:  # #73 fail-closed (TA-02)
            return false()
        o = _org_val(scope)
        return true() if o is None else (m.id == o)
    if s == "own_customer":
        return m.id.in_(scope.customers)
    if s == "own_project":
        return true() if scope.projects is None else m.id.in_(scope.projects)
    if s == "customer_col":
        return _cust_clause(m, scope, global_bearing=ex.global_bearing)
    if s == "memory_customer":
        # #371: the customer partition PLUS the project-visibility boundary.
        return _memory_cust_clause(m, scope)
    if s == "project_col":
        return _proj_clause(m.project_id, scope, not_null=ex.tm_not_null)
    if s == "cust_or_proj":
        return _cust_or_proj_clause(m, scope, global_bearing=ex.global_bearing)
    if s == "doc_col":
        return m.document_id.in_(_documents_subq(scope))
    if s == "lqa":
        return or_(
            m.document_id.in_(_documents_subq(scope)),
            m.segment_id.in_(_segments_subq(scope)),
        )
    if s == "glossary_col":
        base = m.glossary_id.in_(_glossaries_subq(scope))
        if m is TermCandidate:
            base = and_(
                base,
                or_(
                    m.source_document_id.is_(None), m.source_document_id.in_(_documents_subq(scope))
                ),
            )
        return base
    if s == "entry_col":
        return m.glossary_entry_id.in_(_glossary_entries_subq(scope))
    if s == "concept_col":
        return m.concept_id.in_(_glossary_concepts_subq(scope))
    if s == "workflow_col":
        return m.workflow_id.in_(_workflows_subq(scope))
    if s == "wf_or_doc":
        return and_(
            m.document_id.in_(_documents_subq(scope)),
            or_(
                m.workflow_id.in_(_workflows_subq(scope)),
                m.workflow_id.in_(_global_workflows_subq(scope)),
            ),
        )
    if s == "steprun":
        return m.workflow_run_id.in_(_workflow_runs_subq(scope))
    if s == "thread_col":
        return m.thread_id.in_(_query_threads_subq(scope))
    if s == "memory_col":
        return and_(
            m.memory_id.in_(_memory_subq(scope)), m.event_id.in_(_memory_events_subq(scope))
        )
    if s == "suggestion_col":
        return and_(
            m.suggestion_id.in_(_suggestions_subq(scope)), _suggestion_evidence_clause(scope)
        )
    if s == "connection_col":
        return m.connection_id.in_(_connections_subq(scope))
    if s == "connection_ws":
        return _cust_or_proj_clause(m, scope, global_bearing=True)
    if s == "org_col":
        return _org_clause(m, scope)
    if s == "user_org":
        return m.user_id.in_(_org_users_subq(scope))
    if s == "project_member":
        return true() if scope.projects is None else m.project_id.in_(scope.projects)
    if s == "tap_action":
        return _tap_action_clause(scope)
    if s == "tap_run":
        return m.action_id.in_(_tap_actions_subq(scope))
    if s == "vendor":
        base = m.id.in_(_vendorcustomer_subq(scope))
        o = _org_val(scope)
        if scope.include_org_shared and o is not None:
            base = or_(base, m.org_id == o)
        return base
    if s == "cust_or_doc":
        return or_(
            and_(m.document_id.is_(None), m.customer_id.in_(scope.customers)),
            m.document_id.in_(_documents_subq(scope)),
        )
    if s == "eval_dataset":
        return _eval_dataset_clause(scope)
    if s == "eval_dataset_child":
        return m.dataset_id.in_(_eval_datasets_subq(scope))
    if s == "eval_run":
        return _eval_run_clause(scope)
    if s == "eval_run_child":
        return m.run_id.in_(_eval_runs_subq(scope))
    raise ValueError(f"unknown extractor strategy: {s!r}")  # pragma: no cover


def _iter_entity(session, ex: Extractor, scope: ResolvedScope, history: str, options: _Options):
    clause = _scope_clause(ex, scope)
    if history == "current":
        clause = _current_narrow(ex, clause)
    base = select(ex.model).where(clause)
    for batch in _keyset_batches(session, ex.model, base, ex.cursor_col):
        for row in batch:
            yield _row_to_dict(ex, row, options)


def walk(session, scope: ResolvedScope, *, history: str, options: _Options) -> Iterator[tuple]:
    """The single flat row source: yields ``(entity_name, dict)`` across the whole registry,
    keyset-paginated + confined + redacted. The emitters (JSONL here; CSV/XLSX in Train C) tee
    off THIS one stream (LAW4)."""
    for ex in applicable_extractors(scope, history, options):
        for d in _iter_entity(session, ex, scope, history, options):
            yield ex.name, d


def _generate(session, row: DataExportRun, scope, history, options, progress) -> dict:
    from app.ports import exportstore

    counts: dict = {}
    dropped: dict = {}
    members: list = []
    exs = applicable_extractors(scope, history, options)
    total = len(exs)
    max_bytes = int(settings.get("data_export_max_bytes", "5368709120") or 0)
    total_bytes = 0
    partial = False
    capped = False

    # Emitter set (LAW4/§8.1): JSONL is always the machine-first backbone; CSV/XLSX are OPTIONAL
    # human views selected per-run, teed off the SAME per-entity row stream (never a second walk,
    # never a serializer-per-entity). ZipFile permits only ONE open member at a time, so the human
    # emitters buffer to disk siblings (CSV) / a write_only workbook (XLSX) during the JSONL walk
    # and are added to the ZIP after it. The audit slice (D4/LAW5) is workspace + admin-toggle only.
    emitters_req = set(row.emitters or ["jsonl"])
    human = (
        _HumanEmitters(want_csv="csv" in emitters_req, want_xlsx="xlsx" in emitters_req)
        if ("csv" in emitters_req or "xlsx" in emitters_req)
        else None
    )
    want_audit = bool(row.include_audit) and scope.scope_kind == "workspace"

    # TA-08: spool the plaintext ZIP INSIDE the 0700 EXPORTS_DIR (mkstemp is 0600), never in the
    # DATA_DIR root -- so the largest plaintext object is covered by the DX-208 filesystem posture.
    # TA-07 (NOTE for Train C's reaper): a hard kill can strand this `dataexport_*.tmp` sibling in
    # EXPORTS_DIR (put_stream's own temp too); the retention reaper ALSO unlinks stale
    # `dataexport_*.tmp` files past the grace ceiling, since nothing else reaps them.
    _ensure_exports_dir()
    fd, tmp_path = tempfile.mkstemp(
        prefix="dataexport_", suffix=".tmp", dir=str(config.EXPORTS_DIR)
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for done, ex in enumerate(exs, start=1):
                arcname = _JSONL.member_name(ex.name)
                cols = _emitter_columns(ex, options) if human else None
                if human:
                    human.begin_entity(ex.name, cols)
                hasher = hashlib.sha256()
                nbytes = 0
                nrows = 0
                err = None
                with zf.open(arcname, "w") as zh:
                    try:
                        for d in _iter_entity(session, ex, scope, history, options):
                            b = _JSONL.serialize_row(d)
                            zh.write(b)
                            hasher.update(b)
                            nbytes += len(b)
                            nrows += 1
                            total_bytes += len(b)
                            if human:  # tee this row to the CSV/XLSX views (same columns as JSONL)
                                human.row(cols, d)
                            if max_bytes and total_bytes > max_bytes:
                                capped = True
                                err = f"size cap {max_bytes} exceeded"
                                break
                    except Exception as exc:  # one entity failing -> partial, never abort (§5.1)
                        err = secrets_scrub.scrub_secrets(f"{type(exc).__name__}: {exc}")
                        # TA-06: log the SCRUBBED string, never the raw exc (whose message can
                        # carry a secret -- e.g. a key value in a malformed-row error).
                        log.warning("data_export entity %s failed: %s", ex.name, err)
                if human:
                    human.end_entity(ex.name)
                counts[ex.name] = nrows
                members.append(
                    {
                        "path": arcname,
                        "kind": "jsonl",
                        "bytes": nbytes,
                        "sha256": hasher.hexdigest(),
                        "truncated": False,
                    }
                )
                if err:
                    dropped[ex.name] = err
                    partial = True
                if progress:
                    progress(done, total, f"{ex.name}: {nrows}")
                if capped:
                    break

            # Admin audit slice (D4/LAW5/§8.2): workspace + include_audit ONLY -- emitted RAW
            # (LAW3 carve-out DX-08: row_hash is computed over the un-scrubbed fields) with a
            # verify_chain result stamped. A self/customer/project artifact NEVER carries it.
            audit_slice = {"included": False, "rows": 0, "chain_verify": None}
            if want_audit and not capped:
                try:
                    audit_slice = _write_audit_slice(zf, scope, members)
                except Exception as exc:
                    # The audit slice has the same partial-never-abort contract as
                    # other entities. Contention is unverified, not an invalid chain.
                    err = secrets_scrub.scrub_secrets(f"{type(exc).__name__}: {exc}")
                    log.warning("data_export audit slice failed: %s", err)
                    dropped["audit"] = err
                    partial = True
                    audit_slice = {
                        "included": any(m["path"] == "audit.jsonl" for m in members),
                        "rows": None,
                        "chain_verify": None,
                        "error": err,
                    }
                row.chain_verify = json.dumps(audit_slice.get("chain_verify") or {})

            # Human-view members (CSV/XLSX): no ZIP member is open now, so add them + record their
            # per-member sha256/bytes (a truncated XLSX sheet marks the member `truncated:true`).
            if human:
                _add_human_members(zf, human, members)

            manifest = build_manifest(
                row.run_uuid, scope, history, counts, dropped, members, audit_slice=audit_slice
            )
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            zf.writestr("manifest.json", manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

        key = _artifact_key(row.run_uuid)
        artifact_bytes = exportstore.get().put_stream(key, _file_chunks(tmp_path))
    finally:
        if human:
            human.cleanup()
        try:  # noqa: SIM105
            os.unlink(tmp_path)
        except OSError:
            pass

    return {
        "status": DataExportStatus.PARTIAL if partial else DataExportStatus.OK,
        "artifact_key": key,
        "artifact_bytes": artifact_bytes,
        "manifest_sha256": manifest_sha,
        "counts": counts,
        "dropped": dropped,
        "members": [m["path"] for m in members],
    }


@admitted_work
def _execute_job(payload: dict, progress=None) -> dict:
    """jobs.HANDLERS["data_export"] -- re-derive the confinement SERVER-SIDE from the persisted
    DataExportRun row (NEVER the caller-supplied payload, TA-01) and produce + stage the artifact.

    Reachable with an attacker-forged payload via POST /api/jobs/run, so the payload is treated as
    a hint only: the scope tuple is asserted against the row and every id-set is recomputed from the
    row via _resolve_scope_from_row. The options that shape WHICH tables are emitted
    (history/include_audit/include_embeddings/include_eval) are read from the RBAC-written row
    columns, not the payload."""
    run_uuid = payload["run_uuid"]

    with Session(engine) as session:
        row = session.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None:  # pragma: no cover -- the caller always creates it first
            raise ValueError(f"no DataExportRun for run_uuid {run_uuid!r}")
        _assert_payload_matches_row(payload, row)
        scope = _resolve_scope_from_row(session, row)
        options = _Options(
            include_eval=bool(row.include_eval),
            include_embeddings=bool(row.include_embeddings),
        )
        history = row.history
        actor = row.actor

        # #483: claim the run with an atomic QUEUED -> RUNNING compare-and-set. A plain
        # read-then-write here revived a run an erasure / delete had already flipped to
        # `purged` (or `expired`) while it waited in the queue, and a forged job payload that
        # reused a finished run_uuid re-ran it. Zero rows matched = not ours to run: do nothing.
        from sqlalchemy import update as _sa_claim

        claim = session.exec(
            _sa_claim(DataExportRun)
            .where(
                DataExportRun.run_uuid == run_uuid,
                DataExportRun.status == DataExportStatus.QUEUED,
            )
            .values(status=DataExportStatus.RUNNING)
        ).rowcount
        session.commit()
        if not claim:
            session.expire_all()
            current = session.exec(
                select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)
            ).first()
            _audit(
                "data_export.skipped",
                scope,
                actor,
                detail=f"run not queued (status={getattr(current, 'status', None)})",
            )
            return {
                "run_uuid": run_uuid,
                "status": getattr(current, "status", None) or DataExportStatus.PURGED,
                "artifact_key": "",
                "artifact_bytes": 0,
                "counts": {},
                "dropped": {},
                "members": [],
            }
        _audit("data_export.start", scope, actor)
        session.expire_all()
        row = session.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        try:
            result = _generate(session, row, scope, history, options, progress)
        except Exception as exc:
            # #483: only a run that is still RUNNING may become failed; a purge that landed
            # mid-generation must stay purged.
            from sqlalchemy import update as _sa_fail

            session.rollback()
            session.exec(
                _sa_fail(DataExportRun)
                .where(
                    DataExportRun.run_uuid == run_uuid,
                    DataExportRun.status == DataExportStatus.RUNNING,
                )
                .values(
                    status=DataExportStatus.FAILED,
                    error=secrets_scrub.scrub_secrets(f"{type(exc).__name__}: {exc}")[:500],
                    finished_at=datetime.utcnow(),
                )
            )
            session.commit()
            _audit("data_export.failed", scope, actor, detail=str(exc))
            raise
        # DX-207 cancellation cooperation: a GDPR erase / delete covering this scope may have
        # flipped the row to `purged` WHILE we were generating (mark_covering_purged cancels an
        # in-flight run so it cannot re-stage erased content). Finalize with a COMPARE-AND-SET
        # on status == running (gate i432): the in-session `row` is a stale identity-map copy
        # (its attributes loaded before the delete committed), so a plain read-then-write here
        # resurrected a purged run to ok with the erased content staged and downloadable. The
        # conditional UPDATE is atomic on SQLite and Postgres; zero rows matched means the run
        # was cancelled, so the just-staged artifact is dropped and the row is left as it is.
        from sqlalchemy import update as _sa_update

        session.commit()  # end the generation transaction before the guarded finalize
        hours = int(settings.get("data_export_retention_hours", "72") or 72)
        now = datetime.utcnow()
        claimed = session.exec(
            _sa_update(DataExportRun)
            .where(
                DataExportRun.run_uuid == run_uuid,
                DataExportRun.status == DataExportStatus.RUNNING,
            )
            .values(
                status=result["status"],
                artifact_key=result["artifact_key"],
                artifact_bytes=result["artifact_bytes"],
                manifest_sha256=result["manifest_sha256"],
                entity_counts=result["counts"],
                dropped_counts=result["dropped"],
                finished_at=now,
                expires_at=now + timedelta(hours=hours),
            )
        ).rowcount
        session.commit()
        if not claimed:
            from app.ports import exportstore

            try:
                exportstore.get().delete(result["artifact_key"])
            except Exception:
                # Erasure outranks this run: a surviving artifact is erased content left behind.
                log.warning(
                    "data export: could not delete artifact %s staged during an erase",
                    result.get("artifact_key"),
                    exc_info=True,
                )
            session.expire_all()
            row = session.exec(
                select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)
            ).first()
            final = getattr(row, "status", None) or DataExportStatus.PURGED
            _audit("data_export.purged", scope, actor, detail="cancelled mid-run by erase")
            return {
                "run_uuid": run_uuid,
                "status": final,
                "artifact_key": "",
                "artifact_bytes": 0,
                "counts": result["counts"],
                "dropped": result["dropped"],
                "members": result["members"],
            }
    _audit(f"data_export.{result['status']}", scope, actor)
    return {
        "run_uuid": run_uuid,
        "status": result["status"],
        "artifact_key": result["artifact_key"],
        "artifact_bytes": result["artifact_bytes"],
        "counts": result["counts"],
        "dropped": result["dropped"],
        "members": result["members"],
    }


# ======================================================================================
# TRAIN C -- human-view emitters (CSV/XLSX), the admin audit slice, the retention reaper,
# and the GDPR-erase artifact-purge hook (SPEC-data-export §8). All APPEND-only: no edit to
# Train A's walker (`walk`/`_iter_entity`) or scope resolver -- these ride the same stream.
# ======================================================================================

# --------------------------------------------------------------------------------------
# §8.1 Human-view emitters (CSV / XLSX) -- teed off the ONE walk (LAW4). Columns are the
# post-redaction JSONL keys, so a human view can never carry a field the machine view drops.
# --------------------------------------------------------------------------------------
_XLSX_MAX_ROWS = 1_048_576  # openpyxl / Excel hard ceiling (rows per sheet, header included)
_ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # chars openpyxl rejects in a cell


def _emitter_columns(ex: Extractor, options: _Options) -> list:
    """The projected column list for one extractor -- IDENTICAL to the key set `_row_to_dict`
    emits (all model columns minus drop_cols, minus the conditional embedding), in table order.
    So a CSV header / XLSX header row == the JSONL keys (the §8.1 "columns == JSONL keys" law)."""
    drop = set(ex.drop_cols)
    if ex.model is SegmentEmbedding and not options.include_embeddings:
        drop.add("embedding")
    return [c.name for c in ex.model.__table__.columns if c.name not in drop]


def _xlsx_cell(v, worksheet=None):
    from app.output_safety import csv_safe_cell, xlsx_text_cell

    v = _flat_cell(v)
    if isinstance(v, str):
        v = _ILLEGAL_XLSX_RE.sub("", v)
        if worksheet is not None:
            from openpyxl.cell import WriteOnlyCell

            return xlsx_text_cell(WriteOnlyCell(worksheet), v)
        return csv_safe_cell(v)
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return str(v)


class _HumanEmitters:
    """Side-channel CSV/XLSX writers that ride the JSONL walk (LAW4). A ZIP allows only ONE open
    member at a time (the JSONL member during the walk), so CSV rows go to a per-entity temp
    sibling and XLSX rows to a write_only workbook; both are added to the ZIP after the walk by
    `_add_human_members`. One workbook, one sheet per entity; a sheet past Excel's row ceiling is
    truncated with the member flagged `truncated:true` (DX-17-Q4), JSONL staying the source of
    truth."""

    def __init__(self, *, want_csv: bool, want_xlsx: bool) -> None:
        self.want_csv = want_csv
        self.want_xlsx = want_xlsx
        self.csv_members: list = []  # [(arcname, temp_path)]
        self._csv_fh = None
        self._csv_writer = None
        self._csv_tmp = None
        self._csv_arc = None
        self.wb = None
        self.xlsx_path = None
        self._ws = None
        self._ws_rows = 0
        self._ws_truncated = False
        self.xlsx_truncated_any = False
        self._sheet_names: set = set()
        if want_xlsx:
            import openpyxl  # lazy: openpyxl 3.1.5 is vendored (§2.6), never imported by default

            self.wb = openpyxl.Workbook(write_only=True)
            fd, self.xlsx_path = tempfile.mkstemp(
                prefix="dataexport_", suffix=".xlsx.tmp", dir=str(config.EXPORTS_DIR)
            )
            os.close(fd)

    def _sheet_name(self, entity: str) -> str:
        # Excel sheet titles are <=31 chars and forbid []:*?/\; entity names are [a-z_], so only
        # the length + uniqueness need enforcing.
        base = entity[:31] or "sheet"
        name = base
        i = 1
        while name in self._sheet_names:
            suffix = f"_{i}"
            name = base[: 31 - len(suffix)] + suffix
            i += 1
        self._sheet_names.add(name)
        return name

    def begin_entity(self, entity: str, cols: list) -> None:
        if self.want_csv:
            fd, self._csv_tmp = tempfile.mkstemp(
                prefix="dataexport_", suffix=".csv.tmp", dir=str(config.EXPORTS_DIR)
            )
            os.close(fd)
            self._csv_arc = f"{entity}.csv"
            # RFC-4180 CRLF, the reports.to_csv idiom (§2.1g).
            self._csv_fh = open(  # noqa: SIM115 -- kept open across row() calls, closed in end_entity
                self._csv_tmp, "w", newline="", encoding="utf-8"
            )
            self._csv_writer = csv.writer(self._csv_fh)
            self._csv_writer.writerow(cols)
        if self.want_xlsx:
            self._ws = self.wb.create_sheet(self._sheet_name(entity))
            self._ws.append([_xlsx_cell(c, self._ws) for c in cols])  # header row
            self._ws_rows = 1
            self._ws_truncated = False

    def row(self, cols: list, d: dict) -> None:
        vals = [d.get(c) for c in cols]
        if self.want_csv and self._csv_writer is not None:
            self._csv_writer.writerow([_csv_cell(v) for v in vals])
        if self.want_xlsx and self._ws is not None and not self._ws_truncated:
            if self._ws_rows >= _XLSX_MAX_ROWS:
                self._ws_truncated = True  # sheet full -> stop; JSONL remains authoritative
                self.xlsx_truncated_any = True
            else:
                self._ws.append([_xlsx_cell(v, self._ws) for v in vals])
                self._ws_rows += 1

    def end_entity(self, entity: str) -> None:
        if self.want_csv and self._csv_fh is not None:
            self._csv_fh.close()
            self.csv_members.append((self._csv_arc, self._csv_tmp))
            self._csv_fh = self._csv_writer = self._csv_tmp = self._csv_arc = None
        self._ws = None

    def save_xlsx(self) -> None:
        if self.want_xlsx and self.wb is not None:
            self.wb.save(self.xlsx_path)

    def cleanup(self) -> None:
        # Best-effort: close/unlink any temp files (the ZIP already has their bytes by now).
        try:
            if self._csv_fh is not None:
                self._csv_fh.close()
        except Exception:  # noqa: S110 -- temp CSV close; the ZIP already holds its bytes
            pass
        for _arc, path in self.csv_members:
            try:  # noqa: SIM105
                os.unlink(path)
            except OSError:
                pass
        if self.want_xlsx:
            try:  # noqa: SIM105
                self.wb.close()
            except Exception:  # noqa: S110 -- temp workbook close; the ZIP already holds its bytes
                pass
            if self.xlsx_path:
                try:  # noqa: SIM105
                    os.unlink(self.xlsx_path)
                except OSError:
                    pass


def _add_human_members(zf, human: _HumanEmitters, members: list) -> None:
    """Add the buffered CSV siblings + the single XLSX workbook to the ZIP (no member is open
    now) and record each as a manifest member with its own sha256/bytes."""
    for arcname, temp_path in human.csv_members:
        sha, nbytes = _sha256_and_size(temp_path)
        zf.write(temp_path, arcname)
        members.append(
            {"path": arcname, "kind": "csv", "bytes": nbytes, "sha256": sha, "truncated": False}
        )
    if human.want_xlsx:
        human.save_xlsx()
        sha, nbytes = _sha256_and_size(human.xlsx_path)
        zf.write(human.xlsx_path, "export.xlsx")
        members.append(
            {
                "path": "export.xlsx",
                "kind": "xlsx",
                "bytes": nbytes,
                "sha256": sha,
                "truncated": human.xlsx_truncated_any,
            }
        )
