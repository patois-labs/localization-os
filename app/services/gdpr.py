"""GDPR data-subject rights: export (Art. 15/20) and erasure/anonymize (Art. 17).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _log, _BATCH, _EMAIL_RE, _EMAIL_CHAR, _anchored,
_mask_unless_subject, _tombstone, _org_scoped, _iter_by, _iter_by_any, _iter_by_like,
_settings_subject_keys, _mask_settings_subtree, _json_has_anchored, _iter_customers_by_settings,
_iter_by_json, _json_text, _mask_json_other_emails, _mask_other_emails, _safe_endpoint,
_count_where, _count_where_any,
_scrub_json_jobs, _replace_in_json, _scrub_text_column, _scrub_customer_settings,
_replace_exact_in_json, _ERASE_SWEEP, _ERASE_EXPORT_SECTION, _EXPORT_EXEMPT,
_IDENTITY_SWEPT_ELSEWHERE, _IDENTITY_NOT_SWEPT, _JSON_CONTENT_SWEEP, _JSON_NO_IDENTITY,
_iter_by_json_columns, _scrub_json_columns, erase_user,
purge_export_artifacts_for_document, purge_export_artifacts_for_customer,
mark_export_artifacts_for_project, finish_export_artifact_purge from
app.services.gdpr_common.

EXPORT streams every record that references a user, keyed by the denormalized
EMAIL columns the rest of the model uses for attribution (created_by / actor /
added_by / Invite.email). Records ship as NDJSON, one JSON object per line, each
tagged with its `section`. Secrets never leave: no password_hash, no ApiKey
key_hash, no Connection.secret_enc. Tables are read in bounded batches — the
whole DB is never materialized (mirrors audit.iter_export).

ERASE anonymizes rather than hard-deletes: the User row is scrubbed and every
ENUMERATED denormalized email pointer is rewritten to a per-user tombstone
(f"deleted-user-{uid}@erased.invalid"). The swept columns are the attribution
pointers the app reads back as identity, in three groups:

  * ORIGINAL set: Project/Document/Job/Connection/LanguageSet.created_by,
    SyncLog.actor, MemoryEvent.actor, MemoryEntry.created_by,
    WorkflowStep.assignee_email (a LIVE email SEND target — notifications.py mails
    the review-pause notice here, so a stale value keeps emailing an erased user),
    DatabricksExportRun.actor, TapAction.initiator, ProjectMember.added_by, and
    Invite.email/created_by.
  * WIDENING set (chip task_3c57b418): the ~20 further actor-bearing columns the
    original set left behind — ContentType.created_by, ProjectTmSource.created_by,
    ProjectGlossary.created_by, Glossary.approved_by + Glossary.created_by,
    TermCandidate.decided_by, StyleGuide.approved_by + StyleGuide.created_by,
    WorkflowGroup.created_by, ProjectTemplate.created_by,
    SegmentationProfile.created_by, ContextProfile.created_by,
    WorkflowBatch.created_by, WorkflowRun.created_by, EngineProfile.created_by,
    RoutingRule.created_by, DepartmentBudget.created_by, EvalDataset.created_by,
    EvalCandidate.created_by, EvalRun.created_by, Vendor.created_by,
    SegmentPostEdit.actor (Wave 2 — the seam-1/seam-3 editor's email on a post-edit
    episode; vendor rows carry actor='') — each an EXACT-email column swept with the
    identical email->tombstone treatment, and each ALSO exported (Art.15 symmetry). PLUS
    KeyChangeLog.actor, which is NOT exact-match: its 4th grammar
    ("workflow:wf<id>:<locale> by <email>", keys.py / web/keys.py) EMBEDS the email in
    a larger string, so it is swept SUBSTRING-wise (_scrub_text_column, the Job-JSON
    precedent) — an exact `==` sweep is structurally blind to it.
    WorkflowRun.created_by is a SECOND LIVE SMTP SEND target: vendor_return.py
    passes the run to notifications.on_vendor_return_resume_failed, which mails
    run.created_by when a vendor return fails to resume — so, exactly like
    WorkflowStep.assignee_email, a stale value keeps emailing an erased user and
    the .invalid-domain tombstone turns any future send into a no-op.
    Wave 3 (SPEC-wave3-collaboration §3.6/§6.1) adds FIVE more exact-email columns —
    QueryThread.author / QueryThread.assignee_email / QueryThread.closed_by /
    QueryPost.author, and LQAResult.actor (the reviewer-finding author) — each ALSO
    exported (Art.15 symmetry). QueryThread.assignee_email is a THIRD LIVE SMTP SEND
    target (query notifications mail it), so the .invalid tombstone matters there too.
    QueryPost.body and QueryThread.subject are EXPORTED (a subject's own words) but NOT
    scanned by the sweep — erase rewrites attribution columns, never third parties' free
    text (the Project.name / MemoryEvent.before_text posture); an erased user's own body
    text survives un-attributed. LQAResult.note on a MACHINE row is never scanned or
    exported; on a HUMAN row it is exported (matched via LQAResult.actor) as the subject's
    own authored judgement.
  * JSON / free-form: email that lives INSIDE JSON columns. Job.payload["actor"] /
    Job.result are ANCHORED-SUBSTRING-rewritten (_scrub_json_jobs). Customer.settings —
    a free-form preferences blob holding contact emails under primary_contact /
    technical_contact AND arbitrary /api-merged keys — is swept by a VALUE-LEVEL
    EXACT-match pass over its string leaves (_scrub_customer_settings): exact, not
    substring, so a `notes` value that merely MENTIONS an address is left alone.

SWEEP SEMANTICS, stated precisely: enumerated columns are EXACT-match; Customer.settings
is value-level EXACT; KeyChangeLog.actor and Job.payload/result are SUBSTRING but
ANCHORED — the round-2 fix made every substring path (sweep AND the LIKE-confirm export/
scan walks) use ONE regex, `_anchored(email)`, that matches the subject only as a WHOLE
address (lookarounds forbid an adjacent local-part/domain char). So the previously
documented Job-JSON `.replace()` over-reach (ann@corp.com corrupting joann@corp.com /
ann@corp.com.mx) is CLOSED, not merely tolerated: no substring path can corrupt or leak
a third party whose address contains the subject's. The only intentional non-uniformity
left is exact-vs-anchored, and that is by design (an exact column has no surrounding
text to anchor within).

All email-bearing writes happen in ONE transaction with a single commit, so a
crash rolls back the User-row scrub too and a clean full re-run is always possible
(a half-applied erase would otherwise strand the original email AND read its own
tombstone as the search key on re-run — unrecoverable).

SCHEMA RATCHET (#277, 2026-09): the 2026-08-17 survey below went stale as tables were added
(DataExportRun.actor, ProjectBudget / QualityLens / QualityLensExample / AdvancedMission /
AdvancedRun / GlossaryConcept.created_by, UserGroupMember.added_by, and QualityGoldItem /
QualityGoldLabel.labeler, which falls back to the importing actor's email). All of them are
now in _ERASE_SWEEP and exported. verify_gdpr enumerates every identity-named string/JSON
column in SQLModel.metadata and fails unless it is swept (_ERASE_SWEEP or
_IDENTITY_SWEPT_ELSEWHERE) or listed in _IDENTITY_NOT_SWEPT with a reason, so the survey can
no longer go stale silently. Gate round 1 extended it to EVERY JSON column: each is swept by
content (_JSON_CONTENT_SWEEP: Job.payload/result, JobQueue.payload, EvalDatasetItem.meta),
handled elsewhere (Customer.settings) or listed in _JSON_NO_IDENTITY with a reason.
2026-10-08: the MT formality maps (Customer/Project/Document.formality, ADR 0062) and the
register stamped on a run (WorkflowRun.formality) are configuration and listed there; the
repeat-propagation keys in Segment.meta (#617 item 6) hold segment ids, text and status only.
User.ui_theme (ADR 0063), like ui_locale, is a display preference: exported in the "user"
section, kept on the tombstone row at erase (it identifies no one).

KNOWN-UNSWEPT: NONE as of 2026-08-17, re-verified after the HIGH-1 (KeyChangeLog
composite grammar) and HIGH-3 (Customer.settings) fixes. The widening campaign
(task_3c57b418) folded every actor-bearing / email-shaped column the survey recorded
into the sweep, and a re-survey of app/models.py (every *_by / *actor* / assignee_ /
email column, plus the 23 JSON columns) found no additional identity-writing site
outside the swept set. The email-shaped columns left untouched are DELIBERATE and
non-attribution: free-text/content columns that may embed an address (Project.name,
Document.filename, MemoryEvent.before_text/after_text/note, StyleGuide.body, ...) —
never scanned, matching the long-standing posture — and the audit chain (below).

KNOWN-RESIDUE (operator-typed-content caveats, same class as free-text columns above
— reviewer 1 verified NO code writes identity into them, so they are NOT swept):
three free-form JSON sinks an operator COULD hand-type an address into —
Vendor.config, StringKey.custom, and the Document / ContextProfile context notes.
No product write path puts a user's email there; if an operator does, it is
free-text they authored, out of the automated attribution sweep's scope (a document,
not a pointer), exactly like a name typed into Project.name.

The audit log is DELIBERATELY untouched: its actor field is inside a keyed hash
chain (see audit.py), so mutating historic rows would break verify_chain — and
retaining that history is itself a legal obligation (GDPR Art. 17(3)(b)).
Erasure is therefore append-only: a single "user.erase" event is logged; the
original email is NOT stored (a truncated sha256 of an email is reversible), only
a non-reversible reference (the uid + a timestamp) so the trail can be reconciled
without re-exposing the address.

The SAME append-only rationale covers the FORWARDED-ACTOR emails the Identity
trust-hardening train writes into the audit `detail` column as
`on-behalf-of=<email>` (main.py audit middleware, auth_kind=="trust"). Those
addresses are sealed inside the HMAC'd hash chain like every other detail, so they
are PERMANENT under Art. 17(3)(b) and are NOT rewritten by erasure. They are kept
out of the subject-access EXPORT instead: export_user structurally redacts the
whole `on-behalf-of=<...>` token (see the F-5 note there), which covers every
shape security.validate_email accepts — including the bare-word / no-TLD /
non-ASCII / angle-bracket local-part shapes _EMAIL_RE (and thus _mask_other_emails)
misses — so no forwarded address leaks through an export even though the chain
retains it.

Documented in: docs/architecture/data-and-migrations.md
"""

import logging
import re
from datetime import datetime

from sqlalchemy import Text, func, update
from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from app.db import engine
from app.models import (
    AdvancedMission,
    AdvancedRun,
    ApiKey,
    Connection,
    ContentType,
    ContextProfile,
    Customer,
    CustomFieldDef,
    DatabricksExportRun,
    DataExportRun,
    DeliveryEmptyApproval,
    DepartmentBudget,
    Document,
    EngineProfile,
    EvalCandidate,
    EvalDataset,
    EvalDatasetItem,
    EvalRun,
    Glossary,
    GlossaryConcept,
    Invite,
    Job,
    KeyChangeLog,
    KeyImage,
    LanguageSet,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemorySuggestion,
    NotifyChannel,
    NotifyRoute,
    Project,
    ProjectAutomation,
    ProjectBudget,
    ProjectFieldValue,
    ProjectGlossary,
    ProjectMember,
    ProjectTemplate,
    ProjectTmSource,
    QualityGoldItem,
    QualityGoldLabel,
    QualityLens,
    QualityLensExample,
    QueryPost,
    QueryThread,
    Role,
    RoutingRule,
    SegmentationProfile,
    SegmentPostEdit,
    StyleGuide,
    SyncLog,
    TapAction,
    TermCandidate,
    User,
    UserGroup,
    UserGroupMember,
    UserMfa,
    Vendor,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, audit
from app.services.gdpr_common import (
    _BATCH,
    _EMAIL_CHAR,
    _EMAIL_RE,
    _ERASE_EXPORT_SECTION,
    _ERASE_SWEEP,
    _EXPORT_EXEMPT,
    _IDENTITY_NOT_SWEPT,
    _IDENTITY_SWEPT_ELSEWHERE,
    _JSON_CONTENT_SWEEP,
    _JSON_NO_IDENTITY,
    _anchored,
    _count_where,
    _count_where_any,
    _iter_by,
    _iter_by_any,
    _iter_by_json,
    _iter_by_json_columns,
    _iter_by_like,
    _iter_customers_by_settings,
    _json_has_anchored,
    _json_text,
    _mask_json_other_emails,
    _mask_other_emails,
    _mask_settings_subtree,
    _mask_unless_subject,
    _org_scoped,
    _replace_exact_in_json,
    _replace_in_json,
    _safe_endpoint,
    _scrub_customer_settings,
    _scrub_json_columns,
    _scrub_json_jobs,
    _scrub_text_column,
    _settings_subject_keys,
    _tombstone,
    erase_user,
    finish_export_artifact_purge,
    mark_export_artifacts_for_project,
    purge_export_artifacts_for_customer,
    purge_export_artifacts_for_document,
)

__all__ = [
    "_BATCH",
    "_EMAIL_CHAR",
    "_EMAIL_RE",
    "_ERASE_EXPORT_SECTION",
    "_ERASE_SWEEP",
    "_EXPORT_EXEMPT",
    "_IDENTITY_NOT_SWEPT",
    "_IDENTITY_SWEPT_ELSEWHERE",
    "_JSON_CONTENT_SWEEP",
    "_JSON_NO_IDENTITY",
    "Customer",
    "Role",
    "Text",
    "WorkflowStep",
    "_anchored",
    "_count_where",
    "_count_where_any",
    "_iter_by",
    "_iter_by_any",
    "_iter_by_json",
    "_iter_by_json_columns",
    "_iter_by_like",
    "_iter_customers_by_settings",
    "_json_has_anchored",
    "_json_text",
    "_mask_json_other_emails",
    "_mask_other_emails",
    "_mask_settings_subtree",
    "_mask_unless_subject",
    "_org_scoped",
    "_replace_exact_in_json",
    "_replace_in_json",
    "_safe_endpoint",
    "_scrub_customer_settings",
    "_scrub_json_columns",
    "_scrub_json_jobs",
    "_scrub_text_column",
    "_settings_subject_keys",
    "_tombstone",
    "apikeys",
    "datetime",
    "erase_user",
    "finish_export_artifact_purge",
    "func",
    "mark_export_artifacts_for_project",
    "purge_export_artifacts_for_customer",
    "purge_export_artifacts_for_document",
    "sa_delete",
    "select",
    "update",
]

_log = logging.getLogger("loc_tms.gdpr")


class _ExportScope:
    """The org confinement of a scoped export (`caller_org_id` set) for the sections whose
    model carries no org_id of its own (gate multiorg-inv round 1, LOW-2): a moved user's
    copy must not carry the former org's projects, documents or job payloads. Each method
    returns a predicate that keeps a row only when the project, document, connection,
    glossary or thread it hangs off belongs to the caller's org. The ids come from
    subqueries on the rows' own stored org, never from the subject's current org. Built
    only when a caller org is in force; the unscoped export passes no predicate at all."""

    def __init__(self, org_id: int):
        self.org_id = org_id
        self._projects = select(Project.id).where(Project.org_id == org_id)
        self._documents = select(Document.id).where(Document.project_id.in_(self._projects))

    def project(self, col):
        return col.in_(self._projects)

    def document(self, col):
        return col.in_(self._documents)

    def connection(self, col):
        return col.in_(select(Connection.id).where(Connection.org_id == self.org_id))

    def glossary(self, col):
        return col.in_(select(Glossary.id).where(Glossary.org_id == self.org_id))

    def thread(self, col):
        return col.in_(select(QueryThread.id).where(QueryThread.project_id.in_(self._projects)))

    def user_group(self, col):
        return col.in_(select(UserGroup.id).where(UserGroup.org_id == self.org_id))

    def dataset_item(self, col):
        datasets = select(EvalDataset.id).where(EvalDataset.org_id == self.org_id)
        return col.in_(select(EvalDatasetItem.id).where(EvalDatasetItem.dataset_id.in_(datasets)))

    def gold_item(self, col):
        items = select(EvalDatasetItem.id).where(
            EvalDatasetItem.dataset_id.in_(
                select(EvalDataset.id).where(EvalDataset.org_id == self.org_id)
            )
        )
        return col.in_(select(QualityGoldItem.id).where(QualityGoldItem.dataset_item_id.in_(items)))

    def job(self, rec) -> bool:
        """A Job has no org or project column: its org is the org of the connection,
        project or document its payload names (jobs._org_from_payload, the rows' own
        stored org). A job that names none of them cannot be placed in any org, so a
        scoped export leaves it out (the unscoped operator export still carries it)."""
        from app.services.jobs import _org_from_payload

        return _org_from_payload(rec.get("payload")) == self.org_id


def _scoped(scope, kind, col):
    """`scope.<kind>(col)` for a scoped export, None (no predicate) for the unscoped one."""
    return None if scope is None else getattr(scope, kind)(col)


def export_user(uid: int, caller_org_id: int | None = None):
    """Yield (section_name, record_dict) pairs describing everything the app
    holds about the user, for streaming NDJSON. Secrets are excluded. Nothing
    is materialized whole — every table streams in bounded batches.

    Multi-org P8: when `caller_org_id` is set (the customer /users/{uid}/export
    route + the CLI `--org` scope pass the caller's org), an org-admin can only
    export a user IN THEIR OWN org — a cross-org target yields NOTHING (the route
    turns the empty stream into a 404). None (the operator / single-tenant path) is
    unscoped, byte-identical to today. The audit section AND every widening section
    that carries an `org_id` (MED-2) are likewise org-confined, so the subject-access
    copy never spills another org's audit rows OR a cross-org CONFIG row (EngineProfile
    / RoutingRule / DepartmentBudget / Vendor / ...) the subject's email happens to
    author. The org-less widening sections (ProjectTmSource / ProjectGlossary /
    KeyChangeLog / TermCandidate / WorkflowRun) and the Project / Document / Job sections
    carry no org key of their own; a scoped export confines them through the project,
    document, connection, glossary or thread they hang off (_ExportScope; gate multiorg-inv
    round 1, LOW-2), so a user moved between orgs never takes the former org's content
    along. The subject's own account row, API keys and page layouts are per-user, not
    org content, and stay. With `caller_org_id` None nothing changes (no predicate is
    added), byte-identical to before."""
    with Session(engine) as s:
        u = s.get(User, uid)
    if not u:
        return
    # Cross-org refusal: a scoped caller must not read a user in another org.
    if caller_org_id is not None and u.org_id != caller_org_id:
        return
    email = u.email
    scope = None if caller_org_id is None else _ExportScope(caller_org_id)

    yield (
        "user",
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "department": u.department,
            # The subject's own display preferences (Art.15: data about them). Erase keeps
            # them on the tombstone row: a UI language or "light"/"dark" identifies no one.
            "ui_locale": u.ui_locale,
            "ui_theme": u.ui_theme,
            "auth_source": u.auth_source,
            "active": u.active,
            "org_id": u.org_id,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            # NOTE: password_hash deliberately omitted
        },
    )

    # #270 (r1-data-06): the password-policy and two-step sign-in METADATA about the
    # subject. Credential material never leaves: no password_hash, no password history,
    # no authenticator secret (secret_enc), no replay step, no recovery-code hashes.
    def _iso(dt):
        return dt.isoformat() if dt else None

    with Session(engine) as s:
        mfa_row = s.exec(select(UserMfa).where(UserMfa.user_id == uid)).first()
    yield (
        "account_security",
        {
            "password_changed_at": _iso(u.password_changed_at),
            "password_must_change": bool(u.password_must_change),
            "mfa_enrolled": mfa_row is not None,
            "mfa_enabled": bool(mfa_row is not None and mfa_row.enabled_at),
            "mfa_enabled_at": _iso(mfa_row.enabled_at) if mfa_row else None,
            "mfa_started_at": _iso(mfa_row.created_at) if mfa_row else None,
            "mfa_last_used_at": _iso(mfa_row.last_used_at) if mfa_row else None,
            "mfa_fail_count": mfa_row.fail_count if mfa_row else 0,
            "mfa_locked_until": _iso(mfa_row.locked_until) if mfa_row else None,
        },
    )

    for rec in _iter_by(
        ApiKey,
        ApiKey.user_id,
        uid,
        lambda k: {
            "id": k.id,
            "name": k.name,
            "prefix": k.prefix,
            "scopes": k.scopes,
            "active": k.active,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used": k.last_used.isoformat() if k.last_used else None,
            # NOTE: key_hash deliberately omitted
        },
    ):
        yield "api_keys", rec

    # #753: consented MCP OAuth grants, metadata only (every code/token digest omitted).
    from app.models import McpOAuthGrant

    for rec in _iter_by(
        McpOAuthGrant,
        McpOAuthGrant.user_id,
        uid,
        lambda g: {
            "id": g.id,
            "client_name": g.client_name,
            "redirect_uri": g.redirect_uri,
            "scopes": g.scopes,
            "resource": g.resource,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "last_used": g.last_used.isoformat() if g.last_used else None,
            "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
        },
    ):
        yield "mcp_oauth_grants", rec

    for rec in _iter_by(
        Project,
        Project.created_by,
        email,
        lambda p: {
            "id": p.id,
            "name": p.name,
            "customer_id": p.customer_id,
            "source_locale": p.source_locale,
            "default_provider": p.default_provider,
            "created_by": p.created_by,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        },
        org_col=Project.org_id,
        org_id=caller_org_id,
    ):
        yield "projects", rec

    for rec in _iter_by(
        Document,
        Document.created_by,
        email,
        lambda d: {
            "id": d.id,
            "project_id": d.project_id,
            "filename": d.filename,
            "fmt": d.fmt,
            "mode": d.mode,
            "status": d.status,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        },
        where=_scoped(scope, "project", Document.project_id),
    ):
        yield "documents", rec

    # Jobs the user created retain their content. Merely being named in another
    # user's JSON grants no access to that job's content (see the projection below).
    def _shape_job(j):
        return {
            "id": j.id,
            "kind": j.kind,
            "status": j.status,
            "message": j.message,
            "error": j.error,
            "created_by": j.created_by,
            "payload": j.payload,
            "result": j.result,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }

    seen_jobs = set()
    for rec in _iter_by(Job, Job.created_by, email, _shape_job):
        seen_jobs.add(rec["id"])
        if scope is None or scope.job(rec):
            yield "jobs", rec

    # #380: a whole-address mention is attribution, not project/customer visibility.
    # Export only the subject and the names of the JSON columns that mention them.
    # Even a matching leaf or JSON key can contain unrelated text, names or salary
    # data, so no raw content, keys, paths or job metadata belongs in this projection.
    # This applies regardless of visibility: named-only jobs never ship full content.
    job_pat = _anchored(email)

    def _shape_named_job(j):
        return {
            "id": j.id,
            "subject": email,
            "relationship": "mentioned",
            "fields": [
                field
                for field in ("payload", "result")
                if _json_has_anchored(getattr(j, field), job_pat)
            ],
        }

    # Resolve org confinement from the original payload, before discarding content.
    for job in _iter_by_json(Job, email, lambda j: j):
        if job.id not in seen_jobs:
            seen_jobs.add(job.id)
            if scope is None or scope.job({"payload": job.payload}):
                yield "jobs", _shape_named_job(job)

    for rec in _iter_by(
        Connection,
        Connection.created_by,
        email,
        lambda c: {
            "id": c.id,
            "kind": c.kind,
            "name": c.name,
            "status": c.status,
            "project_id": c.project_id,
            "created_by": c.created_by,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            # NOTE: secret_enc deliberately omitted
        },
        org_col=Connection.org_id,
        org_id=caller_org_id,
    ):
        yield "connections", rec

    for rec in _iter_by(
        LanguageSet,
        LanguageSet.created_by,
        email,
        lambda ls: {
            "id": ls.id,
            "name": ls.name,
            "source_locale": ls.source_locale,
            "target_locales": ls.target_locales,
            "created_by": ls.created_by,
            "created_at": ls.created_at.isoformat() if ls.created_at else None,
        },
        org_col=LanguageSet.org_id,
        org_id=caller_org_id,
    ):
        yield "language_sets", rec

    # ProjectMember references the subject two ways: user_id (int FK) and
    # added_by (email). They never collide on one value, so stream each.
    def _shape_member(m):
        return {
            "id": m.id,
            "project_id": m.project_id,
            "user_id": m.user_id,
            "added_by": m.added_by,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }

    in_org = _scoped(scope, "project", ProjectMember.project_id)
    for rec in _iter_by(ProjectMember, ProjectMember.user_id, uid, _shape_member, where=in_org):
        yield "project_members", rec
    for rec in _iter_by(ProjectMember, ProjectMember.added_by, email, _shape_member, where=in_org):
        yield "project_members", rec

    # Train L1: the subject's own saved page layouts (card order per page type).
    from app.services import page_layouts as _page_layouts

    for rec in _page_layouts.export_for_user(uid):
        yield "page_layouts", rec

    for rec in _iter_by_any(
        Invite,
        (Invite.email, Invite.created_by),
        email,
        lambda i: {
            "id": i.id,
            "email": i.email,
            "role": i.role,
            "created_by": i.created_by,
            "accepted": i.accepted,
            "created_at": i.created_at.isoformat() if i.created_at else None,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            # NOTE: token deliberately omitted (a live credential)
        },
        org_col=Invite.org_id,
        org_id=caller_org_id,
    ):
        yield "invites", rec

    for rec in _iter_by(
        SyncLog,
        SyncLog.actor,
        email,
        lambda sl: {
            "id": sl.id,
            "connection_id": sl.connection_id,
            "direction": sl.direction,
            "ok": sl.ok,
            "count": sl.count,
            "message": sl.message,
            "actor": sl.actor,
            "ts": sl.ts.isoformat() if sl.ts else None,
        },
        where=_scoped(scope, "connection", SyncLog.connection_id),
    ):
        yield "sync_logs", rec

    # Art.15/Art.17 SYMMETRY: erase_user now proves MemoryEvent.actor /
    # MemoryEntry.created_by / DatabricksExportRun.actor / TapAction.initiator are
    # subject attribution, so the export must ALSO surface them (the subject is
    # entitled to the same rows erasure will rewrite). Shapes mirror the SyncLog
    # stream — attribution + coarse context, no secrets. (WorkflowStep.assignee_email
    # is swept but deliberately NOT exported here: a WorkflowStep row is workflow
    # internals, not the subject's own content; the address is already visible to the
    # subject as their review assignment. Flagged for the coordinator's judgment.)
    for rec in _iter_by(
        MemoryEvent,
        MemoryEvent.actor,
        email,
        lambda me: {
            "id": me.id,
            "kind": me.kind,
            "project_id": me.project_id,
            "document_id": me.document_id,
            "segment_id": me.segment_id,
            "actor": me.actor,
            "created_at": me.created_at.isoformat() if me.created_at else None,
        },
        org_col=MemoryEvent.org_id,
        org_id=caller_org_id,
    ):
        yield "memory_events", rec

    # Art.15 symmetry: a memory the subject AUTHORED (created_by) OR ADJUDICATED
    # (adjudicated_by, via resolve_conflict — an action the subject took, the
    # query_thread_closers precedent) exports here, deduped by id (build-gate finding 4).
    for rec in _iter_by_any(
        MemoryEntry,
        (MemoryEntry.created_by, MemoryEntry.adjudicated_by),
        email,
        lambda men: {
            "id": men.id,
            "kind": men.kind,
            "status": men.status,
            "customer_id": men.customer_id,
            "created_by": men.created_by,
            "adjudicated_by": men.adjudicated_by,
            "adjudicated_at": men.adjudicated_at.isoformat() if men.adjudicated_at else None,
            "created_at": men.created_at.isoformat() if men.created_at else None,
        },
        org_col=MemoryEntry.org_id,
        org_id=caller_org_id,
    ):
        yield "memory_entries", rec

    # Phase-3 Train A: MemorySuggestion carries the subject in up to THREE actor columns
    # (created_by/reviewed_by/applied_by). Stream the union, deduped by id; a column that is
    # NOT the subject holds a THIRD party (or "system") and is masked unconditionally
    # (_mask_unless_subject), the Glossary/StyleGuide two-column posture. Content columns
    # (rationale/diff_text/proposed_change) are workflow-config prose, not the subject's own
    # authored content, so they are not exported here (the attribution + coarse context shape).
    for rec in _iter_by_any(
        MemorySuggestion,
        (
            MemorySuggestion.created_by,
            MemorySuggestion.reviewed_by,
            MemorySuggestion.applied_by,
        ),
        email,
        lambda ms: {
            "id": ms.id,
            "kind": ms.kind,
            "status": ms.status,
            "customer_id": ms.customer_id,
            "workflow_id": ms.workflow_id,
            "created_by": _mask_unless_subject(ms.created_by, email),
            "reviewed_by": _mask_unless_subject(ms.reviewed_by, email),
            "applied_by": _mask_unless_subject(ms.applied_by, email),
            "created_at": ms.created_at.isoformat() if ms.created_at else None,
        },
        org_col=MemorySuggestion.org_id,
        org_id=caller_org_id,
    ):
        yield "memory_suggestions", rec

    for rec in _iter_by(
        DatabricksExportRun,
        DatabricksExportRun.actor,
        email,
        lambda r: {
            "id": r.id,
            "run_uuid": r.run_uuid,
            "target": r.target,
            "status": r.status,
            "actor": r.actor,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        },
        org_col=DatabricksExportRun.org_id,
        org_id=caller_org_id,
    ):
        yield "databricks_runs", rec

    # TapAction.id is a STRING (UUID) PK, so seed the keyset walk with "" not 0.
    for rec in _iter_by(
        TapAction,
        TapAction.initiator,
        email,
        lambda t: {
            "id": t.id,
            "kind": t.kind,
            "surface": t.surface,
            "status": t.status,
            "project_id": t.project_id,
            "document_id": t.document_id,
            "workflow_name": t.workflow_name,
            "initiator": t.initiator,
            "started_at": t.started_at.isoformat() if t.started_at else None,
        },
        cursor0="",
        org_col=TapAction.org_id,
        org_id=caller_org_id,
    ):
        yield "tap_actions", rec

    # Art.15 SYMMETRY — WIDENING (task_3c57b418): every column the erase now sweeps
    # surfaces in this export (the subject is entitled to the same rows erasure will
    # rewrite), with ONE deliberate exception kept from the original set —
    # WorkflowStep.assignee_email is SWEPT but NOT exported (a WorkflowStep row is
    # workflow internals, not the subject's own content, and the address is already
    # visible to the subject as their review assignment; flagged for the coordinator's
    # judgment). Shapes are attribution + coarse context, no secrets:
    # EngineProfile.api_key_enc / Vendor.secret_enc (Fernet blobs) and the per-kind
    # config/rate_card blobs that may carry paired secrets are omitted, mirroring the
    # connections stream. Sections carrying an `org_id` are org-confined when a caller
    # org is set (MED-2); the org-less ones (ProjectTmSource/ProjectGlossary/
    # KeyChangeLog/TermCandidate/WorkflowRun — project/glossary-scoped) pass no org key.
    # All these models carry an integer id PK (keyset walk seeds at 0), except the audit
    # + Customer.settings scans which key on their own columns.
    for rec in _iter_by(
        ContentType,
        ContentType.created_by,
        email,
        lambda ct: {
            "id": ct.id,
            "name": ct.name,
            "customer_id": ct.customer_id,
            "org_id": ct.org_id,
            "created_by": ct.created_by,
            "created_at": ct.created_at.isoformat() if ct.created_at else None,
        },
        org_col=ContentType.org_id,
        org_id=caller_org_id,
    ):
        yield "content_types", rec

    for rec in _iter_by(
        CustomFieldDef,
        CustomFieldDef.created_by,
        email,
        lambda d: {
            "id": d.id,
            "key": d.key,
            "type": d.type,
            "customer_id": d.customer_id,
            "org_id": d.org_id,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        },
        org_col=CustomFieldDef.org_id,
        org_id=caller_org_id,
    ):
        yield "custom_field_defs", rec

    for rec in _iter_by(
        ProjectFieldValue,
        ProjectFieldValue.updated_by,
        email,
        lambda x: {
            "id": x.id,
            "project_id": x.project_id,
            "field_id": x.field_id,
            "updated_by": x.updated_by,
            "updated_at": x.updated_at.isoformat() if x.updated_at else None,
        },
        where=_scoped(scope, "project", ProjectFieldValue.project_id),
    ):
        yield "project_field_values", rec

    for rec in _iter_by(
        ProjectTmSource,
        ProjectTmSource.created_by,
        email,
        lambda x: {
            "id": x.id,
            "project_id": x.project_id,
            "source_project_id": x.source_project_id,
            "created_by": x.created_by,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        },
        where=_scoped(scope, "project", ProjectTmSource.project_id),
    ):
        yield "project_tm_sources", rec

    for rec in _iter_by(
        ProjectGlossary,
        ProjectGlossary.created_by,
        email,
        lambda x: {
            "id": x.id,
            "project_id": x.project_id,
            "glossary_id": x.glossary_id,
            "created_by": x.created_by,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        },
        where=_scoped(scope, "project", ProjectGlossary.project_id),
    ):
        yield "project_glossaries", rec

    # KeyChangeLog.actor has a composite 4th grammar ("workflow:wf<id>:<locale> by
    # <email>") that embeds the subject's email in a larger string — an exact `==`
    # match is blind to it (HIGH-1), so key on a SUBSTRING (LIKE) walk. The embedded
    # email is the subject's OWN attribution, so it is kept (their data).
    for rec in _iter_by_like(
        KeyChangeLog,
        KeyChangeLog.actor,
        email,
        lambda k: {
            "id": k.id,
            "project_id": k.project_id,
            "entity": k.entity,
            "key_id": k.key_id,
            "namespace": k.namespace,
            "name": k.name,
            "target_locale": k.target_locale,
            "op": k.op,
            "actor": k.actor,
            "changed_at": k.changed_at.isoformat() if k.changed_at else None,
        },
        where=_scoped(scope, "project", KeyChangeLog.project_id),
    ):
        yield "key_change_logs", rec

    # Batch 14: the per-string in-context images this subject UPLOADED. Org-less attribution
    # (KeyImage has no org_id, like KeyChangeLog), so it is matched by uploaded_by only.
    # METADATA ONLY -- the image bytes are NEVER exported (Art.15 covers what is held about
    # the subject; the raw pixels are third-party content the subject merely attached).
    for rec in _iter_by(
        KeyImage,
        KeyImage.uploaded_by,
        email,
        lambda img: {
            "id": img.id,
            "key_id": img.key_id,
            "project_id": img.project_id,
            "caption": img.caption,
            "mime": img.mime,
            "width": img.width,
            "height": img.height,
            "is_primary": img.is_primary,
            "ordinal": img.ordinal,
            "uploaded_at": img.uploaded_at.isoformat() if img.uploaded_at else None,
        },
        where=_scoped(scope, "project", KeyImage.project_id),
    ):
        yield "key_images", rec

    # Batch 15 T2: the TM-only empty deliveries this subject APPROVED. Attribution +
    # tenant-scoped ids + the empty identity set (never source text). Matched by approved_by.
    for rec in _iter_by(
        DeliveryEmptyApproval,
        DeliveryEmptyApproval.approved_by,
        email,
        lambda a: {
            "id": a.id,
            "project_id": a.project_id,
            "document_id": a.document_id,
            "locale": a.locale,
            "status": a.status,
            "covered_count": a.covered_count,
            "approved_at": a.approved_at.isoformat() if a.approved_at else None,
        },
        where=_scoped(scope, "project", DeliveryEmptyApproval.project_id),
    ):
        yield "delivery_empty_approvals", rec

    # Glossary carries the subject in TWO columns (approved_by, created_by); stream the
    # union, deduped by id. MED-1/R2 MED-B: when the subject matched on ONE column, the
    # OTHER holds a THIRD party's email — mask it UNCONDITIONALLY (value != subject ->
    # placeholder) via _mask_unless_subject, regex-free. Both columns are known email
    # attribution fields, so masking must NOT depend on _EMAIL_RE matching their shape
    # (a bare-word / no-TLD address would slip _mask_other_emails).
    for rec in _iter_by_any(
        Glossary,
        (Glossary.approved_by, Glossary.created_by),
        email,
        lambda g: {
            "id": g.id,
            "name": g.name,
            "status": g.status,
            "org_id": g.org_id,
            "customer_id": g.customer_id,
            "approved_by": _mask_unless_subject(g.approved_by, email),
            "created_by": _mask_unless_subject(g.created_by, email),
            "created_at": g.created_at.isoformat() if g.created_at else None,
        },
        org_col=Glossary.org_id,
        org_id=caller_org_id,
    ):
        yield "glossaries", rec

    for rec in _iter_by(
        TermCandidate,
        TermCandidate.decided_by,
        email,
        lambda t: {
            "id": t.id,
            "glossary_id": t.glossary_id,
            "source_term": t.source_term,
            "status": t.status,
            "decided_by": t.decided_by,
            "decided_at": t.decided_at.isoformat() if t.decided_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        },
        where=_scoped(scope, "glossary", TermCandidate.glossary_id),
    ):
        yield "term_candidates", rec

    # StyleGuide also carries the subject in TWO columns (approved_by, created_by) —
    # same union + UNCONDITIONAL non-subject masking + org scope as Glossary (R2 MED-B).
    for rec in _iter_by_any(
        StyleGuide,
        (StyleGuide.approved_by, StyleGuide.created_by),
        email,
        lambda sg: {
            "id": sg.id,
            "name": sg.name,
            "status": sg.status,
            "org_id": sg.org_id,
            "customer_id": sg.customer_id,
            "approved_by": _mask_unless_subject(sg.approved_by, email),
            "created_by": _mask_unless_subject(sg.created_by, email),
            "created_at": sg.created_at.isoformat() if sg.created_at else None,
        },
        org_col=StyleGuide.org_id,
        org_id=caller_org_id,
    ):
        yield "style_guides", rec

    for rec in _iter_by(
        WorkflowGroup,
        WorkflowGroup.created_by,
        email,
        lambda w: {
            "id": w.id,
            "name": w.name,
            "customer_id": w.customer_id,
            "org_id": w.org_id,
            "created_by": w.created_by,
            "created_at": w.created_at.isoformat() if w.created_at else None,
        },
        org_col=WorkflowGroup.org_id,
        org_id=caller_org_id,
    ):
        yield "workflow_groups", rec

    for rec in _iter_by(
        ProjectTemplate,
        ProjectTemplate.created_by,
        email,
        lambda p: {
            "id": p.id,
            "name": p.name,
            "customer_id": p.customer_id,
            "org_id": p.org_id,
            "created_by": p.created_by,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        },
        org_col=ProjectTemplate.org_id,
        org_id=caller_org_id,
    ):
        yield "project_templates", rec

    for rec in _iter_by(
        SegmentationProfile,
        SegmentationProfile.created_by,
        email,
        lambda sp: {
            "id": sp.id,
            "name": sp.name,
            "customer_id": sp.customer_id,
            "org_id": sp.org_id,
            "created_by": sp.created_by,
            "created_at": sp.created_at.isoformat() if sp.created_at else None,
        },
        org_col=SegmentationProfile.org_id,
        org_id=caller_org_id,
    ):
        yield "segmentation_profiles", rec

    for rec in _iter_by(
        ContextProfile,
        ContextProfile.created_by,
        email,
        lambda cp: {
            "id": cp.id,
            "name": cp.name,
            "customer_id": cp.customer_id,
            "content_type_id": cp.content_type_id,
            "project_id": cp.project_id,
            "org_id": cp.org_id,
            "created_by": cp.created_by,
            "created_at": cp.created_at.isoformat() if cp.created_at else None,
        },
        org_col=ContextProfile.org_id,
        org_id=caller_org_id,
    ):
        yield "context_profiles", rec

    for rec in _iter_by(
        WorkflowBatch,
        WorkflowBatch.created_by,
        email,
        lambda b: {
            "id": b.id,
            "workflow_id": b.workflow_id,
            "document_id": b.document_id,
            "status": b.status,
            "created_by": b.created_by,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        },
        org_col=WorkflowBatch.org_id,
        org_id=caller_org_id,
    ):
        yield "workflow_batches", rec

    for rec in _iter_by(
        WorkflowRun,
        WorkflowRun.created_by,
        email,
        lambda r: {
            "id": r.id,
            "workflow_id": r.workflow_id,
            "document_id": r.document_id,
            "target_locale": r.target_locale,
            "status": r.status,
            "created_by": r.created_by,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        },
        where=_scoped(scope, "document", WorkflowRun.document_id),
    ):
        yield "workflow_runs", rec

    # Art.15/Art.17 SYMMETRY (gate M2): SegmentPostEdit.actor is swept by erase_user, so
    # it must ALSO export — a named individual's per-episode post-editing productivity
    # record (edit distances, effort, volume) is first-order subject data. Attribution +
    # coarse context + the derived metrics; NO content: baseline_text / final_sha are
    # deliberately omitted (they are/derive from target text, not the subject's identity).
    for rec in _iter_by(
        SegmentPostEdit,
        SegmentPostEdit.actor,
        email,
        lambda pe: {
            "id": pe.id,
            "segment_id": pe.segment_id,
            "document_id": pe.document_id,
            "target_locale": pe.target_locale,
            "episode_seq": pe.episode_seq,
            "unit_kind": pe.unit_kind,
            "actor": pe.actor,
            "actor_kind": pe.actor_kind,
            "distance_char": pe.distance_char,
            "distance_word": pe.distance_word,
            "distance_pct": pe.distance_pct,
            "cum_distance_pct": pe.cum_distance_pct,
            "edit_count": pe.edit_count,
            "metric_version": pe.metric_version,
            "first_edited_at": pe.first_edited_at.isoformat() if pe.first_edited_at else None,
            "last_edited_at": pe.last_edited_at.isoformat() if pe.last_edited_at else None,
        },
        where=_scoped(scope, "document", SegmentPostEdit.document_id),
    ):
        yield "segment_post_edits", rec

    # Wave 3 (SPEC-wave3-collaboration §3.6) — Art.15 export for the query entity, the
    # MIRROR of the erase sweep below (HIGH-1: every swept column has an export
    # counterpart). A thread the subject opened, was assigned, or closed -> the
    # three-column _iter_by_any match (deduped by id, so a thread they both opened AND
    # closed exports ONCE). `subject` is DELIBERATELY omitted here — this pass also
    # returns threads someone ELSE opened (matched on assignee/closer), and shipping
    # another person's subject line as this subject's data would be wrong (NEW-4).
    for rec in _iter_by_any(
        QueryThread,
        (QueryThread.author, QueryThread.assignee_email, QueryThread.closed_by),
        email,
        lambda q: {
            "id": q.id,
            "project_id": q.project_id,
            "document_id": q.document_id,
            "segment_id": q.segment_id,
            "key_id": q.key_id,
            "target_locale": q.target_locale,
            "status": q.status,
            "author": q.author,
            "author_kind": q.author_kind,
            "assignee_email": q.assignee_email,
            "closed_by": q.closed_by,
            "post_count": q.post_count,
            "created_at": q.created_at.isoformat() if q.created_at else None,
            "updated_at": q.updated_at.isoformat() if q.updated_at else None,
            "closed_at": q.closed_at.isoformat() if q.closed_at else None,
        },
        where=_scoped(scope, "project", QueryThread.project_id),
    ):
        yield "query_threads", rec

    # The subject's OWN opening lines. A SECOND cheap pass keyed on `author` ALONE, so it
    # can never return someone else's subject (NEW-4). `subject` lives on QueryThread, so
    # the QueryPost export below cannot reach it — without this pass a thread-opener's own
    # subject line would export NOWHERE.
    for rec in _iter_by(
        QueryThread,
        QueryThread.author,
        email,
        lambda q: {
            "id": q.id,
            "subject": q.subject,
            "created_at": q.created_at.isoformat() if q.created_at else None,
        },
        where=_scoped(scope, "project", QueryThread.project_id),
    ):
        yield "query_thread_subjects", rec

    # QueryPost: `body` IS included — unlike SegmentPostEdit.baseline_text (translated
    # content), a query post is the data subject's OWN authored statement, first-order
    # Art.15 data (§3.6). author_label / vendor_id / dispatch_id describe a DIFFERENT
    # party's leg and are not exported on a human match.
    for rec in _iter_by(
        QueryPost,
        QueryPost.author,
        email,
        lambda p: {
            "id": p.id,
            "thread_id": p.thread_id,
            "body": p.body,
            "author": p.author,
            "author_kind": p.author_kind,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        },
        where=_scoped(scope, "thread", QueryPost.thread_id),
    ):
        yield "query_posts", rec

    # LQAResult reviewer findings (§6.1). Matches on `actor`, so it can only ever return
    # HUMAN rows (a machine row has actor IS NULL). `note` IS included — on a human row it
    # is the subject's own authored judgement (same argument as QueryPost.body). A machine
    # row's note is model-authored content about someone else's translation and is never
    # scanned or exported (same posture as MemoryEvent.before_text).
    for rec in _iter_by(
        LQAResult,
        LQAResult.actor,
        email,
        lambda r: {
            "id": r.id,
            "document_id": r.document_id,
            "segment_id": r.segment_id,
            "dimension": r.dimension,
            "severity": r.severity,
            "note": r.note,
            "target_locale": r.target_locale,
            "finding_source": r.finding_source,
            "actor": r.actor,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "withdrawn_at": r.withdrawn_at.isoformat() if r.withdrawn_at else None,
        },
        where=_scoped(scope, "document", LQAResult.document_id),
    ):
        yield "lqa_findings", rec

    for rec in _iter_by(
        EngineProfile,
        EngineProfile.created_by,
        email,
        lambda e: {
            "id": e.id,
            "name": e.name,
            "kind": e.kind,
            "model": e.model,
            "enabled": e.enabled,
            "org_id": e.org_id,
            "created_by": e.created_by,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            # NOTE: api_key_enc + config (may hold access_key_secret_enc) omitted
        },
        org_col=EngineProfile.org_id,
        org_id=caller_org_id,
    ):
        yield "engine_profiles", rec

    for rec in _iter_by(
        RoutingRule,
        RoutingRule.created_by,
        email,
        lambda r: {
            "id": r.id,
            "priority": r.priority,
            "active": r.active,
            "match_ext": r.match_ext,
            "department": r.department,
            "project_id": r.project_id,
            "customer_id": r.customer_id,
            "org_id": r.org_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        },
        org_col=RoutingRule.org_id,
        org_id=caller_org_id,
    ):
        yield "routing_rules", rec

    for rec in _iter_by(
        DepartmentBudget,
        DepartmentBudget.created_by,
        email,
        lambda d: {
            "id": d.id,
            "department": d.department,
            "monthly_token_limit": d.monthly_token_limit,
            "org_id": d.org_id,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        },
        org_col=DepartmentBudget.org_id,
        org_id=caller_org_id,
    ):
        yield "department_budgets", rec

    for rec in _iter_by(
        EvalDataset,
        EvalDataset.created_by,
        email,
        lambda d: {
            "id": d.id,
            "name": d.name,
            "org_id": d.org_id,
            "customer_id": d.customer_id,
            "source_locale": d.source_locale,
            "target_locale": d.target_locale,
            "item_count": d.item_count,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        },
        org_col=EvalDataset.org_id,
        org_id=caller_org_id,
    ):
        yield "eval_datasets", rec

    for rec in _iter_by(
        EvalCandidate,
        EvalCandidate.created_by,
        email,
        lambda c: {
            "id": c.id,
            "name": c.name,
            "org_id": c.org_id,
            "customer_id": c.customer_id,
            "workflow_id": c.workflow_id,
            "locked": c.locked,
            "created_by": c.created_by,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        },
        org_col=EvalCandidate.org_id,
        org_id=caller_org_id,
    ):
        yield "eval_candidates", rec

    for rec in _iter_by(
        EvalRun,
        EvalRun.created_by,
        email,
        lambda r: {
            "id": r.id,
            "dataset_id": r.dataset_id,
            "candidate_id": r.candidate_id,
            "org_id": r.org_id,
            "customer_id": r.customer_id,
            "status": r.status,
            "provider": r.provider,
            "model": r.model,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        },
        org_col=EvalRun.org_id,
        org_id=caller_org_id,
    ):
        yield "eval_runs", rec

    # Vendor.endpoint_url may embed CREDENTIALS: vendors._validate_endpoint historically
    # only checked scheme==https, so a userinfo (`https://user:pass@host`), query-secret
    # (`?apikey=X`), OR path-embedded webhook secret (`/hooks/<token>`) endpoint could be
    # stored verbatim (HIGH-2). Emit scheme+host[:port] ONLY (R2 LOW-B: drop the path too
    # — a webhook-style path token would otherwise export verbatim). A boot-time reject of
    # userinfo endpoints is the separate defence-in-depth commit in _validate_endpoint.
    for rec in _iter_by(
        Vendor,
        Vendor.created_by,
        email,
        lambda v: {
            "id": v.id,
            "name": v.name,
            "org_id": v.org_id,
            "status": v.status,
            "endpoint_url": _safe_endpoint(v.endpoint_url),
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            # NOTE: secret_enc + config + rate_card omitted (secret / MSA cost blobs)
        },
        org_col=Vendor.org_id,
        org_id=caller_org_id,
    ):
        yield "vendors", rec

    # #277: the attribution columns added after the 2026-08-17 survey (the Art.15 half of
    # the sweep rows in _ERASE_SWEEP). Attribution + coarse context only: no artifact keys,
    # error text, briefs, example source/target text or other content columns.
    for rec in _iter_by(
        DataExportRun,
        DataExportRun.actor,
        email,
        lambda r: {
            "id": r.id,
            "run_uuid": r.run_uuid,
            "org_id": r.org_id,
            "scope_kind": r.scope_kind,
            "scope_ref": r.scope_ref,
            "history": r.history,
            "status": r.status,
            "actor": r.actor,
            "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at),
        },
        org_col=DataExportRun.org_id,
        org_id=caller_org_id,
    ):
        yield "data_export_runs", rec

    for rec in _iter_by(
        ProjectBudget,
        ProjectBudget.created_by,
        email,
        lambda b: {
            "id": b.id,
            "project_id": b.project_id,
            "org_id": b.org_id,
            "monthly_usd_limit": b.monthly_usd_limit,
            "created_by": b.created_by,
            "created_at": _iso(b.created_at),
        },
        org_col=ProjectBudget.org_id,
        org_id=caller_org_id,
    ):
        yield "project_budgets", rec

    for rec in _iter_by(
        ProjectAutomation,
        ProjectAutomation.created_by,
        email,
        lambda a: {
            "id": a.id,
            "project_id": a.project_id,
            "org_id": a.org_id,
            "connection_id": a.connection_id,
            "created_by": a.created_by,
            "created_at": _iso(a.created_at),
        },
        org_col=ProjectAutomation.org_id,
        org_id=caller_org_id,
    ):
        yield "project_automations", rec

    for rec in _iter_by(
        QualityLens,
        QualityLens.created_by,
        email,
        lambda ql: {
            "id": ql.id,
            "slug": ql.slug,
            "title": ql.title,
            "kind": ql.kind,
            "org_id": ql.org_id,
            "customer_id": ql.customer_id,
            "created_by": ql.created_by,
            "created_at": _iso(ql.created_at),
        },
        org_col=QualityLens.org_id,
        org_id=caller_org_id,
    ):
        yield "quality_lenses", rec

    for rec in _iter_by(
        QualityLensExample,
        QualityLensExample.created_by,
        email,
        lambda x: {
            "id": x.id,
            "lens_id": x.lens_id,
            "org_id": x.org_id,
            "customer_id": x.customer_id,
            "source_locale": x.source_locale,
            "target_locale": x.target_locale,
            "created_by": x.created_by,
            "created_at": _iso(x.created_at),
        },
        org_col=QualityLensExample.org_id,
        org_id=caller_org_id,
    ):
        yield "quality_lens_examples", rec

    for rec in _iter_by(
        AdvancedMission,
        AdvancedMission.created_by,
        email,
        lambda m: {
            "id": m.id,
            "name": m.name,
            "project_id": m.project_id,
            "org_id": m.org_id,
            "customer_id": m.customer_id,
            "created_by": m.created_by,
            "created_at": _iso(m.created_at),
        },
        org_col=AdvancedMission.org_id,
        org_id=caller_org_id,
    ):
        yield "advanced_missions", rec

    for rec in _iter_by(
        AdvancedRun,
        AdvancedRun.created_by,
        email,
        lambda r: {
            "id": r.id,
            "mission_id": r.mission_id,
            "project_id": r.project_id,
            "org_id": r.org_id,
            "status": r.status,
            "created_by": r.created_by,
            "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at),
        },
        org_col=AdvancedRun.org_id,
        org_id=caller_org_id,
    ):
        yield "advanced_runs", rec

    for rec in _iter_by(
        GlossaryConcept,
        GlossaryConcept.created_by,
        email,
        lambda c: {
            "id": c.id,
            "glossary_id": c.glossary_id,
            "external_id": c.external_id,
            "created_by": c.created_by,
            "created_at": _iso(c.created_at),
        },
        where=_scoped(scope, "glossary", GlossaryConcept.glossary_id),
    ):
        yield "glossary_concepts", rec

    # UserGroupMember references the subject two ways, like ProjectMember: user_id (their
    # own directory group memberships) and added_by (edges they added).
    def _shape_group_member(m):
        return {
            "id": m.id,
            "group_id": m.group_id,
            "user_id": m.user_id,
            "added_by": m.added_by,
            "created_at": _iso(m.created_at),
        }

    in_org_groups = _scoped(scope, "user_group", UserGroupMember.group_id)
    for rec in _iter_by(
        UserGroupMember, UserGroupMember.user_id, uid, _shape_group_member, where=in_org_groups
    ):
        yield "user_group_members", rec
    for rec in _iter_by(
        UserGroupMember,
        UserGroupMember.added_by,
        email,
        _shape_group_member,
        where=in_org_groups,
    ):
        if rec["user_id"] != uid:  # already yielded by the user_id pass
            yield "user_group_members", rec

    for rec in _iter_by(
        QualityGoldItem,
        QualityGoldItem.labeler,
        email,
        lambda g: {
            "id": g.id,
            "dataset_item_id": g.dataset_item_id,
            "gold_score": g.gold_score,
            "locked": g.locked,
            "labeler": g.labeler,
            "created_at": _iso(g.created_at),
        },
        where=_scoped(scope, "dataset_item", QualityGoldItem.dataset_item_id),
    ):
        yield "quality_gold_items", rec

    for rec in _iter_by(
        QualityGoldLabel,
        QualityGoldLabel.labeler,
        email,
        lambda g: {
            "id": g.id,
            "gold_item_id": g.gold_item_id,
            "kind": g.kind,
            "severity": g.severity,
            "category": g.category,
            "note": g.note,
            "labeler": g.labeler,
            "created_at": _iso(g.created_at),
        },
        where=_scoped(scope, "gold_item", QualityGoldLabel.gold_item_id),
    ):
        yield "quality_gold_labels", rec

    # Project automation Part B (review gate 10-13): notification channels and routes the
    # subject created, and email channels that list the subject as a recipient (only the
    # subject's own address is emitted; never the destination URL or signing secret).
    for rec in _iter_by(
        NotifyChannel,
        NotifyChannel.created_by,
        email,
        lambda c: {
            "id": c.id,
            "name": c.name,
            "kind": c.kind,
            "customer_id": c.customer_id,
            "org_id": c.org_id,
            "created_by": c.created_by,
            "created_at": _iso(c.created_at),
        },
        org_col=NotifyChannel.org_id,
        org_id=caller_org_id,
    ):
        yield "notify_channels", rec

    for rec in _iter_by(
        NotifyRoute,
        NotifyRoute.created_by,
        email,
        lambda r: {
            "id": r.id,
            "channel_id": r.channel_id,
            "scope": r.scope,
            "scope_ref": r.scope_ref,
            "org_id": r.org_id,
            "created_by": r.created_by,
            "created_at": _iso(r.created_at),
        },
        org_col=NotifyRoute.org_id,
        org_id=caller_org_id,
    ):
        yield "notify_routes", rec

    rcpt_pat = _anchored(email)
    for rec in _iter_by_json_columns(
        NotifyChannel,
        (NotifyChannel.config,),
        email,
        lambda c: {
            "id": c.id,
            "kind": c.kind,
            "recipients": [
                r
                for r in ((c.config or {}).get("recipients") or [])
                if isinstance(r, str) and rcpt_pat.fullmatch(r)
            ],
        },
        where=(NotifyChannel.org_id == caller_org_id) if caller_org_id is not None else None,
    ):
        yield "notify_channel_recipients", rec

    # Gate fix-gdpr round 1 (M2): gold-rule import stamps `rules_labeler` (falling back to
    # the importing actor's email) into EvalDatasetItem.meta. Emit ONLY the meta keys that
    # name the subject, using the same minimal projection as Customer.settings.
    item_pat = _anchored(email)
    for rec in _iter_by_json_columns(
        EvalDatasetItem,
        (EvalDatasetItem.meta,),
        email,
        lambda it: {
            "id": it.id,
            "dataset_id": it.dataset_id,
            "meta": _settings_subject_keys(it.meta, item_pat, email),
        },
        where=_scoped(scope, "dataset_item", EvalDatasetItem.id),
    ):
        yield "dataset_item_labels", rec

    # HIGH-3: Customer.settings is a free-form JSON blob holding CONTACT EMAILS
    # (primary_contact / technical_contact from the 360 admin form, plus arbitrary keys
    # merged by POST /api/customers/{cid}/settings + the CLI). An erased user listed as a
    # contact is the subject's own occurrence, so it must surface here. R2 MED-B/C
    # #380: emit ONLY the customer id and projected subject occurrence paths. Customer
    # names, unrelated keys and surrounding values are foreign data, not subject data.
    # Erase tombstones the subject's own value (see _scrub_customer_settings), so a
    # post-erase export by anchored-match no longer finds the tombstoned contact keys.
    for rec in _iter_customers_by_settings(
        email,
        lambda c, matched: {
            "id": c.id,
            "settings": matched,  # ONLY the keys naming the subject
        },
        org_id=caller_org_id,
    ):
        yield "customers", rec

    # Audit trail — reuse the SIEM exporter, filtered to this actor. These rows
    # are retained after erasure (legal obligation); include them so the subject
    # still receives the copy the law entitles them to. But target/detail can
    # name OTHER people (e.g. "user.role target=bob@x.com"): mask every email that
    # is NOT the subject's own, so this stays a subject-access copy, not others'
    # PII.
    # Multi-org P8: pass caller_org_id so the audit stream can never spill another
    # org's rows (belt-and-braces on top of the actor=email filter). None with the
    # gate off = unscoped, as today.
    #
    # MED-2 (trust-hardening, F-1/F-B8): the trust-host-auth annotation writes the
    # forwarded end-user into `detail` as `on-behalf-of=<value>`. `_mask_other_emails`
    # alone does NOT cover it: `security.validate_email` (the annotator's gate)
    # deliberately tolerates bare-word logins / intranet addresses / non-ASCII locals /
    # angle-bracket local-parts, none of which `_EMAIL_RE` matches — so those shapes
    # would export RAW. These rows are ALWAYS attributed to the trust principal (the
    # middleware only sets `on-behalf-of` under auth_kind=="trust", where ident["actor"]
    # is the fixed principal — never the forwarded user), and the export filters
    # `actor == subject.email` (audit._filtered), so the `on-behalf-of` value is BY
    # CONSTRUCTION a third party and is never the subject's own data — redact the whole
    # token STRUCTURALLY (after the email mask). The fixed-width lookbehind means a row
    # with no `on-behalf-of=` token is byte-unchanged, so a normal subject-access export
    # is unaffected.
    for rec in audit.iter_export(actor=email, org_id=caller_org_id):
        rec = dict(rec)
        rec["target"] = _mask_other_emails(rec.get("target", ""), email)
        rec["detail"] = re.sub(
            r"(?<=on-behalf-of=)\S+",
            "[redacted-actor]",
            _mask_other_emails(rec.get("detail", ""), email),
            flags=re.IGNORECASE,
        )  # LOW-3: case-insensitive marker match (defence in depth)
        yield "audit", rec
