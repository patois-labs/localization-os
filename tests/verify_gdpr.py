"""GDPR export / erase (anonymize) verification (offline).

- export_user() yields every PII section, with password_hash / key_hash /
  secret_enc absent from the whole stream; the audit section carries the
  subject's rows; the export event itself is audited
- erase_user() anonymizes atomically: every denormalized email column AND every
  email hidden inside Job.payload/result JSON is tombstoned (zero rows left
  matching the old email outside auditlog), the subject's ProjectMember rows are
  removed, the User row is scrubbed and inactive, API keys are revoked, the live
  session dies, the audit chain still verifies, and a user.erase event with a
  NON-reversible (uid+timestamp) detail is appended
- guards: self-erase refused, last-active-admin refused, an erased user can
  still be exported afterward (the tombstone export works)
- export masks OTHER users' emails out of the subject's audit rows
- route gating: /account/export with auth OFF -> 400 error page; the admin
  routes are admin-gated
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_gdpr_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Connection,
    ContentType,
    ContextProfile,
    Customer,
    DatabricksExportRun,
    DepartmentBudget,
    Document,
    EngineProfile,
    EvalCandidate,
    EvalDataset,
    EvalRun,
    Glossary,
    Invite,
    Job,
    JobStatus,
    KeyChangeLog,
    LanguageSet,
    MemoryEntry,
    MemoryEvent,
    MemorySuggestion,
    Project,
    ProjectGlossary,
    ProjectMember,
    ProjectTemplate,
    ProjectTmSource,
    Role,
    RoutingRule,
    SegmentationProfile,
    StyleGuide,
    SyncLog,
    TapAction,
    TermCandidate,
    User,
    Vendor,
    Workflow,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, audit, gdpr, security, settings
from tests._csrf import csrf_for

TARGET = "target@example.com"

# F-5 on-behalf-of markers seeded into audit `detail` (see _seed_pii). The middleware
# format is `on-behalf-of=<value>`.
#   LEGIT shape  — a normal email _EMAIL_RE matches (masked by _mask_other_emails AND
#                  then structurally redacted).
#   FORGED shape — an intranet address with NO TLD dot: _EMAIL_RE requires `.<TLD>`, so
#                  it matches nothing here and _mask_other_emails leaves it RAW — only
#                  the structural `on-behalf-of=` redaction can remove it.
OBO_LEGIT_EMAIL = "obo.legit@third.example.com"
OBO_FORGED_RAW = "secretuser@intranet"  # no dot in the domain -> _EMAIL_RE miss
OBO_LEGIT_DETAIL = f"req=42 on-behalf-of={OBO_LEGIT_EMAIL} done"
OBO_FORGED_DETAIL = f"on-behalf-of={OBO_FORGED_RAW}"

# HIGH-1: KeyChangeLog.actor's composite 4th grammar, EXACT pinned form from
# keys.py / web/keys.py ("workflow:wf%d:%s by %s") — the subject's email is embedded.
KCL_COMPOSITE_ACTOR = f"workflow:wf7:es-ES by {TARGET}"
# HIGH-2: a Vendor endpoint that embeds credentials (userinfo + query secret). The
# export must strip both, keeping scheme+host+path only.
VENDOR_CRED_SENTINELS = ("svcacct", "S3CR3T", "apikey", "SEEKRIT")
# R2 LOW-B: the export emits scheme+host ONLY (path dropped — a webhook path token would
# otherwise leak). The "/intake" path (sentinel: "intk-secret") must NOT survive either.
VENDOR_CRED_ENDPOINT = "https://svcacct:S3CR3T@vendor.example/intk-secret?apikey=SEEKRIT"
VENDOR_ENDPOINT_SAFE = "https://vendor.example"


def _seed_pii(uid: int) -> None:
    """Create at least one row in every PII table attributed to TARGET."""
    with Session(engine) as s:
        # project_id / connection_id / user_id are real FKs: flush the parent rows
        # first and reference their REAL ids instead of a hardcoded id=1 / id=999
        # guess (those never persisted on Postgres, where these FKs are enforced).
        proj = Project(name="P1", created_by=TARGET)
        conn = Connection(
            kind="airtable", name="C1", secret_enc="TOP-SECRET-BLOB", created_by=TARGET
        )
        # A distinct peer user for the second ProjectMember row (added_by=TARGET
        # exercises the "attributed to the subject" masking path). Its email is NOT
        # TARGET's, so the row-count assertions are unaffected.
        peer = User(
            email="peer@example.com",
            name="Peer",
            password_hash=security.hash_password("peer-pass-123456"),
        )
        # A real Customer: MemoryEntry.customer_id is a REQUIRED FK (enforced on
        # Postgres), so flush the parent first and reference its real id below.
        cust = Customer(name="Cust1")
        # A real Workflow: WorkflowStep.workflow_id is a REQUIRED FK.
        wf = Workflow(name="WF1")
        s.add(proj)
        s.add(conn)
        s.add(peer)
        s.add(cust)
        s.add(wf)
        s.commit()
        s.refresh(proj)
        s.refresh(conn)
        s.refresh(peer)
        s.refresh(cust)
        s.refresh(wf)
        # FK parents for the widening-set leaf rows below (a real Document for the
        # WorkflowBatch/WorkflowRun FKs; a real Glossary for ProjectGlossary +
        # TermCandidate; a real EvalDataset + EvalCandidate for EvalRun). gl / gl_appr /
        # ds / cand are THEMSELVES subject-attributed swept rows (Glossary.created_by /
        # approved_by, EvalDataset.created_by, EvalCandidate.created_by).
        doc = Document(project_id=proj.id, filename="parent.docx", created_by="admin@example.com")
        # gl: subject is the creator, a THIRD party is the approver (MED-1 mask target).
        # gl_appr: subject is the approver, a third party is the creator.
        gl = Glossary(name="GL1", created_by=TARGET, approved_by="gl-approver-3p@third.example.com")
        gl_appr = Glossary(
            name="GL-approved", approved_by=TARGET, created_by="gl-author-3p@third.example.com"
        )
        ds = EvalDataset(name="DS1", created_by=TARGET)
        cand = EvalCandidate(name="CAND1", created_by=TARGET)
        s.add(doc)
        s.add(gl)
        s.add(gl_appr)
        s.add(ds)
        s.add(cand)
        s.commit()
        s.refresh(doc)
        s.refresh(gl)
        s.refresh(ds)
        s.refresh(cand)
        s.add(Document(project_id=proj.id, filename="a.docx", created_by=TARGET))
        s.add(Job(kind="translate_document", status=JobStatus.COMPLETED, created_by=TARGET))
        # A connector job where the subject's email lives ONLY inside JSON:
        # payload["actor"] and a result blob (created_by is a DIFFERENT actor).
        s.add(
            Job(
                kind="connector_translate",
                status=JobStatus.COMPLETED,
                created_by="scheduler",
                payload={"actor": TARGET, "connection_id": conn.id},
                result={
                    "ok": True,
                    "message": f"pushed by {TARGET}",
                    "failures": [{"item": "row3", "by": TARGET}],
                },
            )
        )
        s.add(LanguageSet(name="LS1", target_locales=["es-MX"], created_by=TARGET))
        s.add(ProjectMember(project_id=proj.id, user_id=uid, added_by="admin@example.com"))
        s.add(ProjectMember(project_id=proj.id, user_id=peer.id, added_by=TARGET))
        s.add(
            Invite(email=TARGET, role="translator", token="tok-a", created_by="admin@example.com")
        )
        s.add(
            Invite(email="someone@example.com", role="translator", token="tok-b", created_by=TARGET)
        )
        s.add(SyncLog(connection_id=conn.id, actor=TARGET))
        # llmemory episode/belief layer: the subject's email denormalizes into
        # MemoryEvent.actor and MemoryEntry.created_by. Seed one of each attributed
        # to TARGET, PLUS control rows that erasure must NOT touch: a peer actor/
        # author (different email) and a "system"-authored belief (exact-match sweep
        # must skip it).
        s.add(MemoryEvent(kind="edit", actor=TARGET, before_text="x", after_text="y"))
        s.add(MemoryEvent(kind="edit", actor="peer@example.com", before_text="x", after_text="y"))
        s.add(MemoryEntry(customer_id=cust.id, body="belief-1", created_by=TARGET))
        s.add(MemoryEntry(customer_id=cust.id, body="belief-2", created_by="peer@example.com"))
        s.add(MemoryEntry(customer_id=cust.id, body="belief-sys", created_by="system"))
        # WorkflowStep.assignee_email is a LIVE SMTP send target (notifications.py):
        # an erased reviewer must stop receiving pause emails. Seed a target-assigned
        # human_review step plus a peer-assigned control (must stay untouched).
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                step_type="human_review",
                name="Review",
                assignee_email=TARGET,
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                step_type="human_review",
                name="Review2",
                assignee_email="peer@example.com",
            )
        )
        # DatabricksExportRun.actor + TapAction.initiator: SyncLog.actor-class run
        # attribution. One TARGET row + one peer control for each. TapAction.id is a
        # string PK (no default) so it must be supplied.
        s.add(DatabricksExportRun(run_uuid="run-t", actor=TARGET))
        s.add(DatabricksExportRun(run_uuid="run-p", actor="peer@example.com"))
        s.add(TapAction(id="tap-target", initiator=TARGET))
        s.add(TapAction(id="tap-peer", initiator="peer@example.com"))
        # WIDENING SET (task_3c57b418): one TARGET-attributed row in every newly-swept
        # column, PLUS a peer control on a representative subset (must stay untouched by
        # the exact-email sweep). gl/gl_appr/ds/cand above already cover
        # Glossary.created_by / Glossary.approved_by / EvalDataset.created_by /
        # EvalCandidate.created_by.
        s.add(ContentType(name="CT-target", created_by=TARGET))
        s.add(ContentType(name="CT-peer", created_by="peer@example.com"))
        s.add(ProjectTmSource(project_id=proj.id, source_project_id=proj.id, created_by=TARGET))
        s.add(ProjectGlossary(project_id=proj.id, glossary_id=gl.id, created_by=TARGET))
        s.add(KeyChangeLog(project_id=proj.id, key_id=1, actor=TARGET))
        s.add(KeyChangeLog(project_id=proj.id, key_id=2, actor="peer@example.com"))
        # HIGH-1 red-proof: KeyChangeLog.actor's composite 4th grammar embeds the email
        # in a larger string. Use the EXACT pinned grammar (verify_keys_surfaces.py:
        # "workflow:wf%d:%s by %s"). An exact-match sweep/scan is blind to this row.
        s.add(KeyChangeLog(project_id=proj.id, key_id=3, actor=KCL_COMPOSITE_ACTOR))
        s.add(TermCandidate(glossary_id=gl.id, source_term="widget", decided_by=TARGET))
        s.add(TermCandidate(glossary_id=gl.id, source_term="gadget", decided_by="peer@example.com"))
        # MED-1 red-proof: a two-column asset where the subject matched on ONE column and
        # the OTHER column holds a THIRD party's raw email — the export must MASK it.
        s.add(
            StyleGuide(
                name="SG-created", created_by=TARGET, approved_by="approver-3p@third.example.com"
            )
        )
        s.add(
            StyleGuide(
                name="SG-approved", approved_by=TARGET, created_by="author-3p@third.example.com"
            )
        )
        s.add(WorkflowGroup(name="WG1", created_by=TARGET))
        s.add(ProjectTemplate(name="PT1", created_by=TARGET))
        s.add(SegmentationProfile(name="SP1", created_by=TARGET))
        s.add(ContextProfile(name="CP1", created_by=TARGET))
        s.add(WorkflowBatch(workflow_id=wf.id, document_id=doc.id, created_by=TARGET))
        # WorkflowRun.created_by is a LIVE SMTP send target (notifications.on_vendor_
        # return_resume_failed mails run.created_by) — one TARGET row + a peer control.
        s.add(WorkflowRun(workflow_id=wf.id, document_id=doc.id, created_by=TARGET))
        s.add(WorkflowRun(workflow_id=wf.id, document_id=doc.id, created_by="peer@example.com"))
        s.add(EngineProfile(name="EP1", created_by=TARGET))
        s.add(RoutingRule(created_by=TARGET))
        s.add(DepartmentBudget(department="Legal", created_by=TARGET))
        s.add(EvalRun(dataset_id=ds.id, candidate_id=cand.id, created_by=TARGET))
        # HIGH-2 red-proof: a Vendor whose endpoint_url embeds credentials (userinfo +
        # query secret). Seeded via the model directly (bypassing _validate_endpoint, as
        # a pre-existing / slipped-through row) so the EXPORT strip is what's under test.
        s.add(Vendor(name="V1", created_by=TARGET, endpoint_url=VENDOR_CRED_ENDPOINT))
        # HIGH-3 red-proof: Customer.settings carries contact emails. `cust` already
        # exists; give it primary/technical contacts == the subject, a RENAMED/custom
        # contact key (arbitrary /api-merged key) == the subject, a free-text `notes`
        # value that only MENTIONS the subject (exact-match sweep must NOT touch it), and
        # a peer contact (third party, export must mask; erase must not touch).
        cust_row = s.get(Customer, cust.id)
        cust_row.settings = {
            "primary_contact": TARGET,
            "technical_contact": TARGET,
            "billing_owner_email": TARGET,  # arbitrary/renamed contact key
            "notes": f"reach the primary contact at {TARGET} before 5pm",
            "peer_contact": "cust-peer@third.example.com",
        }
        s.add(cust_row)
        s.commit()
    # API keys (via the real minting path so key_hash is populated)
    apikeys.create_key(uid, "target key")
    # Audit rows attributed to the subject. The user.role row names ANOTHER user
    # in target/detail -> the export must mask that other email, keep the subject's.
    audit.log(actor=TARGET, action="login.success", ip="9.9.9.9")
    audit.log(actor=TARGET, action="project.create", target="P1")
    audit.log(
        actor=TARGET,
        action="user.role",
        target="bob@other.example.com",
        detail=f"changed by {TARGET}; peer carol@third.example.com",
    )
    # F-5 (Identity trust-hardening, routed to this chip): the audit middleware writes
    # the forwarded end-user into `detail` as `on-behalf-of=<value>`, sealed in the HMAC
    # hash chain (PERMANENT, Art.17(3)(b)). export_user must STRUCTURALLY redact the
    # whole on-behalf-of token so no forwarded address leaks — for EVERY shape
    # security.validate_email accepts, including the ones _EMAIL_RE (and thus
    # _mask_other_emails) misses. Seed both a LEGIT-shape marker (a normal email
    # _EMAIL_RE matches) and a FORGED/hard shape (an intranet address with no TLD dot
    # that _EMAIL_RE does NOT match, so ONLY the structural redaction can catch it).
    # actor=TARGET so these rows flow into the subject's actor-filtered export; in
    # production on-behalf-of rows are always the trust principal's, never the subject's.
    audit.log(actor=TARGET, action="api.trust", detail=OBO_LEGIT_DETAIL)
    audit.log(actor=TARGET, action="api.trust", detail=OBO_FORGED_DETAIL)


def _no_email_anywhere(email: str) -> dict:
    """For every PII table (except auditlog), count rows still matching `email` —
    INCLUDING email hidden inside Job.payload / Job.result JSON columns."""
    left = {}
    with Session(engine) as s:
        from sqlalchemy import func, or_
        from sqlalchemy import select as sa_select

        checks = [
            ("Project", Project, Project.created_by == email),
            ("Document", Document, Document.created_by == email),
            ("Job", Job, Job.created_by == email),
            ("Connection", Connection, Connection.created_by == email),
            ("LanguageSet", LanguageSet, LanguageSet.created_by == email),
            ("ProjectMember", ProjectMember, ProjectMember.added_by == email),
            ("SyncLog", SyncLog, SyncLog.actor == email),
            ("MemoryEvent", MemoryEvent, MemoryEvent.actor == email),
            ("MemoryEntry", MemoryEntry, MemoryEntry.created_by == email),
            (
                "MemoryEntry.adjudicated_by",
                MemoryEntry,
                MemoryEntry.adjudicated_by == email,
            ),
            # Phase-3 Train A: MemorySuggestion's three actor-email columns.
            (
                "MemorySuggestion",
                MemorySuggestion,
                or_(
                    MemorySuggestion.created_by == email,
                    MemorySuggestion.reviewed_by == email,
                    MemorySuggestion.applied_by == email,
                ),
            ),
            ("WorkflowStep", WorkflowStep, WorkflowStep.assignee_email == email),
            ("DatabricksExportRun", DatabricksExportRun, DatabricksExportRun.actor == email),
            ("TapAction", TapAction, TapAction.initiator == email),
            ("User", User, User.email == email),
            ("Invite", Invite, or_(Invite.email == email, Invite.created_by == email)),
            # --- widening set (task_3c57b418) ---
            ("ContentType", ContentType, ContentType.created_by == email),
            ("ProjectTmSource", ProjectTmSource, ProjectTmSource.created_by == email),
            ("ProjectGlossary", ProjectGlossary, ProjectGlossary.created_by == email),
            # KeyChangeLog.actor is scanned SEPARATELY below (anchored Python confirm),
            # not in this exact-count list — see the dedicated block after the loop.
            (
                "Glossary",
                Glossary,
                or_(Glossary.approved_by == email, Glossary.created_by == email),
            ),
            ("TermCandidate", TermCandidate, TermCandidate.decided_by == email),
            (
                "StyleGuide",
                StyleGuide,
                or_(StyleGuide.approved_by == email, StyleGuide.created_by == email),
            ),
            ("WorkflowGroup", WorkflowGroup, WorkflowGroup.created_by == email),
            ("ProjectTemplate", ProjectTemplate, ProjectTemplate.created_by == email),
            ("SegmentationProfile", SegmentationProfile, SegmentationProfile.created_by == email),
            ("ContextProfile", ContextProfile, ContextProfile.created_by == email),
            ("WorkflowBatch", WorkflowBatch, WorkflowBatch.created_by == email),
            ("WorkflowRun", WorkflowRun, WorkflowRun.created_by == email),
            ("EngineProfile", EngineProfile, EngineProfile.created_by == email),
            ("RoutingRule", RoutingRule, RoutingRule.created_by == email),
            ("DepartmentBudget", DepartmentBudget, DepartmentBudget.created_by == email),
            ("EvalDataset", EvalDataset, EvalDataset.created_by == email),
            ("EvalCandidate", EvalCandidate, EvalCandidate.created_by == email),
            ("EvalRun", EvalRun, EvalRun.created_by == email),
            ("Vendor", Vendor, Vendor.created_by == email),
        ]
        for label, model, cond in checks:
            n = s.exec(sa_select(func.count()).select_from(model).where(cond)).one()[0]
            if n:
                left[label] = n
        # KeyChangeLog.actor: LIKE prefilter then ANCHORED Python confirm, mirroring
        # production (R2 LOW-A: a bare `LIKE %a_b@example.com%` false-REDs on
        # `axb@example.com` because `_` is a LIKE wildcard; and an unanchored confirm
        # false-REDs on a longer address containing the subject's). Import gdpr's own
        # _anchored so the scan can never drift from the sweep's semantics.
        pat = gdpr._anchored(email)
        kcls = (
            s.exec(sa_select(KeyChangeLog).where(KeyChangeLog.actor.like(f"%{email}%")))
            .scalars()
            .all()
        )
        kcl_hits = sum(1 for k in kcls if pat.search(k.actor or ""))
        if kcl_hits:
            left["KeyChangeLog"] = kcl_hits
        # Deep scan of Job JSON blobs (payload["actor"], result, anywhere) — ANCHORED,
        # matching the Job-JSON anchored-substring sweep.
        import json

        jobs = s.exec(sa_select(Job)).scalars().all()
        json_hits = sum(
            1 for j in jobs if pat.search(json.dumps(j.payload or {}) + json.dumps(j.result or {}))
        )
        if json_hits:
            left["Job.json"] = json_hits

        # HIGH-3: Customer.settings — count rows with any string leaf that EXACTLY equals
        # the email (matching the value-level exact-match sweep, so a dropped sweep goes
        # RED). Exact, not substring: a free-text `notes` value that merely MENTIONS the
        # address is deliberately not swept and must not trip the scan.
        def _leaf_exact(blob) -> bool:
            if isinstance(blob, str):
                return blob == email
            if isinstance(blob, dict):
                return any(_leaf_exact(v) for v in blob.values())
            if isinstance(blob, list):
                return any(_leaf_exact(v) for v in blob)
            return False

        custs = s.exec(sa_select(Customer)).scalars().all()
        cust_hits = sum(1 for c in custs if _leaf_exact(c.settings))
        if cust_hits:
            left["Customer.settings"] = cust_hits
    return left


def main() -> None:
    init_db()
    test_380_mention_projections()
    settings.set_many({"audit_enabled": "1"})

    # A real DB-backed user for the subject, plus two admins for the guard tests.
    target = security.create_user(TARGET, "Target User", Role.TRANSLATOR, "target-pass-123")
    admin1 = security.create_user("admin1@example.com", "Admin One", Role.ADMIN, "admin1-pass-123")
    admin2 = security.create_user("admin2@example.com", "Admin Two", Role.ADMIN, "admin2-pass-123")
    _seed_pii(target.id)
    print("  seeded a subject + data across every PII table")

    # =====================================================================
    # EXPORT (service level)
    # =====================================================================
    sections = {}
    for section, rec in gdpr.export_user(target.id):
        sections.setdefault(section, []).append(rec)
    expected = {
        "user",
        "api_keys",
        "projects",
        "documents",
        "jobs",
        "connections",
        "language_sets",
        "project_members",
        "invites",
        "sync_logs",
        "audit",
        "memory_events",
        "memory_entries",
        "databricks_runs",
        "tap_actions",
        # widening set (task_3c57b418): every erase-swept table is exported too
        "content_types",
        "project_tm_sources",
        "project_glossaries",
        "key_change_logs",
        "glossaries",
        "term_candidates",
        "style_guides",
        "workflow_groups",
        "project_templates",
        "segmentation_profiles",
        "context_profiles",
        "workflow_batches",
        "workflow_runs",
        "engine_profiles",
        "routing_rules",
        "department_budgets",
        "eval_datasets",
        "eval_candidates",
        "eval_runs",
        "vendors",
        "customers",
    }  # HIGH-3: Customer.settings contact emails
    missing = expected - set(sections)
    assert not missing, f"export missing sections: {missing}"
    print("  export: all %d sections present" % len(expected))  # noqa: UP031

    # user section excludes password_hash
    assert "password_hash" not in sections["user"][0], "password_hash leaked in user section"
    # api_keys excludes key_hash; connections exclude secret_enc; invites exclude token
    assert all("key_hash" not in k for k in sections["api_keys"]), "key_hash leaked"
    assert all("secret_enc" not in c for c in sections["connections"]), "secret_enc leaked"
    assert all("token" not in i for i in sections["invites"]), "invite token leaked"

    # the WHOLE serialized stream must not contain any secret string/field name
    import json

    blob = "\n".join(
        json.dumps({"section": sec, **rec}) for sec, recs in sections.items() for rec in recs
    )
    for needle in (
        "password_hash",
        "key_hash",
        "secret_enc",
        "TOP-SECRET-BLOB",
        *VENDOR_CRED_SENTINELS,
    ):  # HIGH-2: no vendor endpoint credential
        assert needle not in blob, f"secret {needle!r} present in export output"
    print(
        "  export: password_hash / key_hash / secret_enc / vendor-endpoint creds "
        "absent from the whole stream"
    )

    # project_members: both the user_id match and the added_by match are present
    pm_userids = {m["user_id"] for m in sections["project_members"]}
    pm_addedby = {m["added_by"] for m in sections["project_members"]}
    assert target.id in pm_userids, "membership by user_id missing"
    assert TARGET in pm_addedby, "membership by added_by missing"
    # invites: both the email match and the created_by match are present
    inv_by = {(i["email"], i["created_by"]) for i in sections["invites"]}
    assert any(e == TARGET for e, _ in inv_by), "invite by email missing"
    assert any(cb == TARGET for _, cb in inv_by), "invite by created_by missing"

    # audit section carries the subject's rows
    audit_actions = {r["action"] for r in sections["audit"]}
    assert "login.success" in audit_actions and "project.create" in audit_actions, audit_actions
    assert all(r["actor"] == TARGET for r in sections["audit"]), "audit section not actor-scoped"
    print("  export: audit section carries the subject's rows only")

    # audit export masks OTHER users' emails in target/detail, keeps the subject's
    role_rows = [r for r in sections["audit"] if r["action"] == "user.role"]
    assert role_rows, "seeded user.role audit row missing from export"
    joined = " ".join(r["target"] + " " + r["detail"] for r in role_rows)
    assert "bob@other.example.com" not in joined, "other user's email leaked in audit target"
    assert "carol@third.example.com" not in joined, "other user's email leaked in audit detail"
    assert "[redacted-email]" in joined, "expected masked-email placeholder"
    assert TARGET in joined, "the subject's own email must be preserved in their audit rows"
    print("  export: audit section masks other users' emails, keeps the subject's own")

    # Owned jobs keep payload/result; named-only jobs expose subject metadata only.
    job_recs = sections["jobs"]
    assert any("payload" in j and "result" in j for j in job_recs), "job payload/result omitted"
    conn_jobs = [
        j
        for j in job_recs
        if j.get("relationship") == "mentioned" or j.get("kind") == "connector_translate"
    ]
    assert conn_jobs, "connector job (subject only in payload JSON) missing from export"
    assert "payload" not in conn_jobs[0] and "result" not in conn_jobs[0], (
        "#380: named-only job exports unrelated JSON",
        conn_jobs[0],
    )
    assert conn_jobs[0]["subject"] == TARGET, "connector job subject wrong"
    print("  export: owned job content + minimal JSON-only mention records")

    # Art.15 symmetry: the new attribution streams carry the subject's rows. The
    # tap_actions stream also exercises the string-PK keyset walk (cursor0="").
    assert all(me["actor"] == TARGET for me in sections["memory_events"]), (
        "memory_events export not actor-scoped"
    )
    assert all(men["created_by"] == TARGET for men in sections["memory_entries"]), (
        "memory_entries export not author-scoped"
    )
    assert any(r["actor"] == TARGET for r in sections["databricks_runs"]), (
        "databricks_runs export missing the subject's run"
    )
    tap_ids = {t["id"] for t in sections["tap_actions"]}
    assert "tap-target" in tap_ids and all(
        t["initiator"] == TARGET for t in sections["tap_actions"]
    ), "tap_actions export (string PK) missing/leaking — cursor0 keyset walk broken"
    print(
        "  export: memory_events/memory_entries/databricks_runs/tap_actions "
        "carry the subject's rows (string-PK walk OK)"
    )

    # -- WIDENING (task_3c57b418): every newly-swept table is symmetrically exported,
    # keyed by the same attribution column(s), carrying the subject's rows only.
    _by = lambda sec, col: [r for r in sections[sec] if r.get(col) == TARGET]  # noqa: E731
    assert _by("content_types", "created_by"), "content_types export missing subject"
    assert _by("project_tm_sources", "created_by"), "project_tm_sources export missing"
    assert _by("project_glossaries", "created_by"), "project_glossaries export missing"
    assert _by("key_change_logs", "actor"), "key_change_logs export missing subject"
    assert _by("term_candidates", "decided_by"), "term_candidates export missing subject"
    assert _by("workflow_groups", "created_by"), "workflow_groups export missing"
    assert _by("project_templates", "created_by"), "project_templates export missing"
    assert _by("segmentation_profiles", "created_by"), "segmentation_profiles export missing"
    assert _by("context_profiles", "created_by"), "context_profiles export missing"
    assert _by("workflow_batches", "created_by"), "workflow_batches export missing"
    assert _by("workflow_runs", "created_by"), "workflow_runs export missing subject"
    assert _by("engine_profiles", "created_by"), "engine_profiles export missing"
    assert _by("routing_rules", "created_by"), "routing_rules export missing subject"
    assert _by("department_budgets", "created_by"), "department_budgets export missing"
    assert _by("eval_datasets", "created_by"), "eval_datasets export missing subject"
    assert _by("eval_candidates", "created_by"), "eval_candidates export missing"
    assert _by("eval_runs", "created_by"), "eval_runs export missing subject"
    assert _by("vendors", "created_by"), "vendors export missing subject"
    # Glossary + StyleGuide each carry the subject in TWO columns — the union stream
    # must surface BOTH the created_by-match and the approved_by-match rows.
    assert any(g["created_by"] == TARGET for g in sections["glossaries"]) and any(
        g["approved_by"] == TARGET for g in sections["glossaries"]
    ), "glossaries export must carry BOTH created_by and approved_by subject rows"
    assert any(sg["created_by"] == TARGET for sg in sections["style_guides"]) and any(
        sg["approved_by"] == TARGET for sg in sections["style_guides"]
    ), "style_guides export must carry BOTH created_by and approved_by subject rows"
    # secret columns are omitted from the credentialed-model exports
    assert all("api_key_enc" not in e and "config" not in e for e in sections["engine_profiles"]), (
        "engine_profile secret leaked"
    )
    assert all("secret_enc" not in v for v in sections["vendors"]), "vendor secret leaked"
    print(
        "  export: all 20 widening sections carry the subject's rows "
        "(Glossary/StyleGuide two-column union OK; engine/vendor secrets omitted)"
    )

    # -- HIGH-1: KeyChangeLog's composite 4th grammar row (email embedded) must SURFACE
    # in the export (LIKE-keyed), carrying the subject's own embedded email.
    kcl_actors = [k["actor"] for k in sections["key_change_logs"]]
    assert KCL_COMPOSITE_ACTOR in kcl_actors, (
        "HIGH-1: composite KeyChangeLog.actor row missing from export (LIKE key broken)",
        kcl_actors,
    )
    assert any(a == TARGET for a in kcl_actors), "HIGH-1: bare KeyChangeLog row missing"
    print("  export: HIGH-1 KeyChangeLog composite-grammar row surfaces (LIKE-keyed)")

    # -- MED-1: in the two-column sections, the NON-subject attribution column holds a
    # third party's email — it MUST be masked, not shipped raw.
    gl_join = " ".join(
        str(g.get("approved_by", "")) + " " + str(g.get("created_by", ""))
        for g in sections["glossaries"]
    )
    sg_join = " ".join(
        str(s.get("approved_by", "")) + " " + str(s.get("created_by", ""))
        for s in sections["style_guides"]
    )
    for third in ("gl-approver-3p@third.example.com", "gl-author-3p@third.example.com"):
        assert third not in gl_join, ("MED-1: third-party glossary attribution leaked", third)
    for third in ("approver-3p@third.example.com", "author-3p@third.example.com"):
        assert third not in sg_join, ("MED-1: third-party style-guide attribution leaked", third)
    assert "[redacted-email]" in gl_join and "[redacted-email]" in sg_join, (
        "MED-1: expected masked placeholder for the non-subject attribution column"
    )
    assert TARGET in gl_join and TARGET in sg_join, (
        "MED-1: the subject's OWN attribution must be preserved"
    )
    print("  export: MED-1 two-column non-subject attribution masked, subject's own kept")

    # -- HIGH-2 / R2 LOW-B: Vendor.endpoint_url reduced to scheme+host ONLY (userinfo +
    # query + PATH all dropped).
    v_ep = [v["endpoint_url"] for v in sections["vendors"]]
    assert VENDOR_ENDPOINT_SAFE in v_ep, ("HIGH-2: vendor endpoint not normalized", v_ep)
    for sentinel in (*VENDOR_CRED_SENTINELS, "intk-secret"):  # incl. the path token
        assert all(sentinel not in ep for ep in v_ep), (
            "HIGH-2/LOW-B: vendor endpoint credential or path token leaked",
            sentinel,
            v_ep,
        )
    print("  export: HIGH-2/LOW-B vendor endpoint stripped to scheme+host (no creds, no path)")

    # -- HIGH-3: Customer.settings contact emails surface (the subject's own), with
    # other keys' third-party addresses masked (MED-1 posture).
    cust_secs = sections["customers"]
    assert cust_secs, "HIGH-3: customers section missing (settings contact not exported)"
    # #380: customer names and surrounding content are not subject data.
    assert all(set(c) == {"id", "settings"} for c in cust_secs), (
        "#380: customers record must be narrowed to id+settings",
        cust_secs[0].keys(),
    )
    cust_blob = json.dumps([c["settings"] for c in cust_secs])
    assert TARGET in cust_blob, "HIGH-3: subject's own contact email missing from export"
    # the peer contact KEY is not emitted at all (dropped, not masked) — structural
    assert "cust-peer@third.example.com" not in cust_blob, (
        "R2 MED-C: unrelated third-party customer contact leaked (should be dropped)"
    )
    assert all("peer_contact" not in c["settings"] for c in cust_secs), (
        "R2 MED-C: unmatched key emitted (structural narrowing failed)"
    )
    # only keys whose value anchored-matches the subject survive
    allowed = {"[redacted-key]", "[redacted-key]#2", "[redacted-key]#3", "[redacted-key]#4"}
    assert all(set(c["settings"]) <= allowed for c in cust_secs), (
        "R2 MED-C: an unmatched settings key rode along",
        cust_secs,
    )
    print(
        "  export: HIGH-3 Customer.settings narrowed to the subject-matching keys "
        "(peer_contact dropped, whole blob NOT emitted)"
    )

    # -- F-5 (Identity trust-hardening, routed to this chip): the audit export must
    # STRUCTURALLY redact the whole on-behalf-of token so NO forwarded address leaks,
    # for both a legit-email shape AND a forged/no-TLD shape _EMAIL_RE misses.
    obo_details = " ".join(r.get("detail", "") for r in sections["audit"])
    assert OBO_LEGIT_EMAIL not in obo_details, (
        "F-5: legit-shape on-behalf-of email leaked through export"
    )
    assert OBO_FORGED_RAW not in obo_details, (
        "F-5: forged-shape on-behalf-of value leaked (structural redaction failed)"
    )
    assert "secretuser" not in obo_details, (
        "F-5: partial forged on-behalf-of value leaked through export"
    )
    assert obo_details.count("on-behalf-of=[redacted-actor]") >= 2, (
        "F-5: both on-behalf-of markers must redact to [redacted-actor]",
        obo_details,
    )
    print(
        "  export: F-5 on-behalf-of detail structurally redacted "
        "(legit + forged/no-TLD shapes both -> [redacted-actor], no raw leak)"
    )

    # =====================================================================
    # EXPORT (route level, auth ON, admin)
    # =====================================================================
    settings.set_many({"security_auth_required": "1"})
    ca = TestClient(app_ref())
    csrf_for(ca)  # auth on: the login POST carries the session CSRF token
    r = ca.post(
        "/auth/login",
        data={"email": "admin1@example.com", "password": "admin1-pass-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    resp = ca.get(f"/users/{target.id}/export")
    assert resp.status_code == 200 and "ndjson" in resp.headers["content-type"], resp.status_code
    assert resp.headers["content-disposition"].endswith('.ndjson"')
    lines = [json.loads(ln) for ln in resp.text.strip().splitlines()]
    assert any(ln["section"] == "user" for ln in lines)
    assert audit.count(action="gdpr.export") >= 1, "admin export not audited"
    # the gdpr.export audit event was appended for the SELF export too (checked below)
    print("  route: admin GET /users/{uid}/export -> NDJSON, audited")

    # self-export as the target user
    ct = TestClient(app_ref())
    csrf_for(ct)  # auth on: the login POST carries the session CSRF token
    assert (
        ct.post(
            "/auth/login",
            data={"email": TARGET, "password": "target-pass-123", "next": "/"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    resp = ct.get("/account/export")
    assert resp.status_code == 200 and "ndjson" in resp.headers["content-type"]
    assert audit.count(action="gdpr.export") >= 2, "self export not audited"
    print("  route: self GET /account/export -> NDJSON, audited")

    # admin routes gated: the target (translator) cannot export other users
    assert ct.get(f"/users/{admin1.id}/export").status_code == 403, "admin export must be gated"
    print("  route: /users/{uid}/export is admin-gated (translator -> 403)")

    # =====================================================================
    # ERASE GUARDS (before the real erase)
    # =====================================================================
    ok, msg = gdpr.erase_user(admin1.id, actor="admin1@example.com")
    assert not ok and "your own" in msg.lower(), (ok, msg)
    print("  guard: self-erase refused")

    # last-active-admin: disable admin2 so admin1 is the only ACTIVE admin, then
    # erasing admin1 must be refused (prove the active-count check, not just role).
    # #21 regression: a refused erase must be a PROVABLE NO-OP. Mint a real API
    # key for admin1 first (via the real minting path, same as _seed_pii does for
    # the target) and confirm it still authenticates AFTER the refusal — the bug
    # was that gdpr.erase_user revoked every key BEFORE the last-admin recheck,
    # so a refused erase silently killed the admin's live keys anyway.
    admin1_key, admin1_full_key = apikeys.create_key(admin1.id, "admin1 standing key")
    assert admin1_key.active, "seeded key should start active"
    assert apikeys.verify_key(admin1_full_key) is not None, (
        "seeded key should authenticate before the refused erase"
    )
    with Session(engine) as s:
        a2 = s.get(User, admin2.id)
        a2.active = False
        s.add(a2)
        s.commit()
    ok, msg = gdpr.erase_user(admin1.id, actor="someoneelse@example.com")
    assert not ok and "last active administrator" in msg.lower(), (ok, msg)
    print("  guard: last-active-admin refused")
    # the refusal must be a provable no-op: the key minted above is still active
    # in the DB, and the admin's User row is untouched (still active, real email).
    live_keys = apikeys.list_keys(admin1.id)
    assert any(k.id == admin1_key.id and k.active for k in live_keys), (
        "#21 REGRESSION: a refused last-admin erase revoked the admin's API key "
        "(side effect on a refused destructive op)"
    )
    assert apikeys.verify_key(admin1_full_key) is not None, (
        "#21 REGRESSION: a refused last-admin erase left the API key unable to "
        "authenticate (revoked before the refusal)"
    )
    with Session(engine) as s:
        a1_row = s.get(User, admin1.id)
        assert a1_row.active and a1_row.email == "admin1@example.com", (
            "#21 REGRESSION: a refused last-admin erase mutated the User row"
        )
    print(
        "  guard: refused last-admin erase left the API key ACTIVE + authenticating "
        "(no side effect) - #21"
    )
    # re-enable admin2 so admin1 stays usable for later route calls
    with Session(engine) as s:
        a2 = s.get(User, admin2.id)
        a2.active = True
        s.add(a2)
        s.commit()

    # =====================================================================
    # ERASE (the real anonymize)
    # =====================================================================
    chain_before = audit.verify_chain()
    assert chain_before[0], "chain must verify before erase"

    ok, msg = gdpr.erase_user(target.id, actor="admin1@example.com", ip="1.1.1.1")
    assert ok, msg
    print(f"  erase: {msg}")

    # every denormalized column tombstoned: zero rows match the old email
    left = _no_email_anywhere(TARGET)
    assert not left, f"email still present after erase: {left}"
    tomb = f"deleted-user-{target.id}@erased.invalid"
    with Session(engine) as s:
        u = s.get(User, target.id)
        assert u.email == tomb and u.name == "" and u.password_hash == "", (u.email, u.name)
        assert u.active is False, "erased user must be inactive"
        # the tombstone now owns the previously-attributed rows
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        proj_n = s.exec(
            sa_select(func.count()).select_from(Project).where(Project.created_by == tomb)
        ).one()[0]
        assert proj_n >= 1, "project attribution not moved to tombstone"
    print("  erase: User scrubbed + inactive; attribution rewritten to the tombstone")

    # API keys revoked
    assert all(not k.active for k in apikeys.list_keys(target.id)), "API keys not revoked"
    print("  erase: all API keys revoked")

    # session dead: the target's live client now 302s to login
    r = ct.get("/account", follow_redirects=False)
    assert r.status_code in (302, 303) and "/auth/login" in r.headers.get("location", ""), (
        r.status_code,
        r.headers.get("location"),
    )
    print("  erase: the erased user's live session is dead (redirect to login)")

    # audit chain still verifies AFTER erase, and the erase event is present
    ok_c, n_c, bad_c = audit.verify_chain()
    assert ok_c and bad_c is None, (ok_c, n_c, bad_c)
    print("  erase: audit.verify_chain() still (True, %d, None) after erase" % n_c)  # noqa: UP031

    erase_rows = [r for r in audit.iter_export(action="user.erase")]  # noqa: C416
    assert erase_rows, "no user.erase audit event"
    ev = erase_rows[-1]
    assert ev["target"] == tomb, ev
    # detail must be a NON-reversible reference (uid + timestamp), NOT a hash of the
    # email (a truncated sha256 of an email is reversible by dictionary attack).
    import hashlib

    bad_prefix = hashlib.sha256(TARGET.encode("utf-8")).hexdigest()[:12]
    assert bad_prefix not in ev["detail"], "erase detail leaks a reversible email hash"
    assert "sha256" not in ev["detail"].lower(), ("erase detail should not be a hash", ev["detail"])
    assert f"uid={target.id}" in ev["detail"], (
        "erase detail should reference the uid",
        ev["detail"],
    )
    print("  erase: user.erase event present with a non-reversible (uid+ts) detail")

    # ProjectMember: the subject's own membership rows (user_id == uid) are gone
    with Session(engine) as s:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        mem_n = s.exec(
            sa_select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.user_id == target.id)
        ).one()[0]
        assert mem_n == 0, "subject's ProjectMember rows not removed on erase"
    print("  erase: subject's ProjectMember rows removed")

    # connector job's JSON is tombstone-clean (payload actor + result)
    with Session(engine) as s:
        import json as _json

        from sqlalchemy import select as sa_select

        cjobs = [
            j for j in s.exec(sa_select(Job)).scalars().all() if j.kind == "connector_translate"
        ]
        assert cjobs, "connector job vanished"
        blob = _json.dumps(cjobs[0].payload or {}) + _json.dumps(cjobs[0].result or {})
        assert TARGET not in blob, "email survived in Job JSON after erase"
        assert cjobs[0].payload["actor"] == tomb, "payload actor not tombstoned"
    print("  erase: Job.payload/result JSON scrubbed to the tombstone")

    # llmemory sweep: MemoryEvent.actor + MemoryEntry.created_by tombstoned; the
    # peer actor/author and the "system"-authored belief are UNTOUCHED (exact match).
    with Session(engine) as s:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        me_tomb = s.exec(
            sa_select(func.count()).select_from(MemoryEvent).where(MemoryEvent.actor == tomb)
        ).one()[0]
        me_peer = s.exec(
            sa_select(func.count())
            .select_from(MemoryEvent)
            .where(MemoryEvent.actor == "peer@example.com")
        ).one()[0]
        assert me_tomb == 1, "MemoryEvent.actor not rewritten to the tombstone"
        assert me_peer == 1, "peer MemoryEvent.actor must be untouched"
        men_tomb = s.exec(
            sa_select(func.count()).select_from(MemoryEntry).where(MemoryEntry.created_by == tomb)
        ).one()[0]
        men_peer = s.exec(
            sa_select(func.count())
            .select_from(MemoryEntry)
            .where(MemoryEntry.created_by == "peer@example.com")
        ).one()[0]
        men_sys = s.exec(
            sa_select(func.count())
            .select_from(MemoryEntry)
            .where(MemoryEntry.created_by == "system")
        ).one()[0]
        assert men_tomb == 1, "MemoryEntry.created_by not rewritten to the tombstone"
        assert men_peer == 1, "peer MemoryEntry.created_by must be untouched"
        assert men_sys == 1, "system-authored MemoryEntry must be untouched (exact match)"
    # the erase banner reports the new sweeps
    assert "memory_events" in msg and "memory_entries" in msg, (
        "erase count banner should report the llmemory sweeps",
        msg,
    )
    print(
        "  erase: MemoryEvent.actor + MemoryEntry.created_by tombstoned; peer + "
        "system rows untouched; banner reports both sweeps"
    )

    # WorkflowStep.assignee_email (live SMTP target), DatabricksExportRun.actor,
    # TapAction.initiator: swept to the tombstone; peer controls untouched.
    with Session(engine) as s:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        ws_tomb = s.exec(
            sa_select(func.count())
            .select_from(WorkflowStep)
            .where(WorkflowStep.assignee_email == tomb)
        ).one()[0]
        ws_peer = s.exec(
            sa_select(func.count())
            .select_from(WorkflowStep)
            .where(WorkflowStep.assignee_email == "peer@example.com")
        ).one()[0]
        assert ws_tomb == 1, (
            "WorkflowStep.assignee_email not rewritten (erased user still emailable)"
        )
        assert ws_peer == 1, "peer WorkflowStep.assignee_email must be untouched"
        dr_tomb = s.exec(
            sa_select(func.count())
            .select_from(DatabricksExportRun)
            .where(DatabricksExportRun.actor == tomb)
        ).one()[0]
        dr_peer = s.exec(
            sa_select(func.count())
            .select_from(DatabricksExportRun)
            .where(DatabricksExportRun.actor == "peer@example.com")
        ).one()[0]
        assert dr_tomb == 1, "DatabricksExportRun.actor not rewritten to the tombstone"
        assert dr_peer == 1, "peer DatabricksExportRun.actor must be untouched"
        ta_tomb = s.exec(
            sa_select(func.count()).select_from(TapAction).where(TapAction.initiator == tomb)
        ).one()[0]
        ta_peer = s.exec(
            sa_select(func.count())
            .select_from(TapAction)
            .where(TapAction.initiator == "peer@example.com")
        ).one()[0]
        assert ta_tomb == 1, "TapAction.initiator not rewritten to the tombstone"
        assert ta_peer == 1, "peer TapAction.initiator must be untouched"
    assert all(k in msg for k in ("workflow_step_assignees", "databricks_runs", "tap_actions")), (
        "erase banner should report the assignee/databricks/tap sweeps",
        msg,
    )
    print(
        "  erase: WorkflowStep.assignee_email + DatabricksExportRun.actor + "
        "TapAction.initiator tombstoned; peer controls untouched; banner reports all"
    )

    # WIDENING SET (task_3c57b418): the whole-DB scan above (_no_email_anywhere)
    # already proved ZERO raw-TARGET occurrences remain across every widening column.
    # Here also prove (a) the tombstone now owns the subject's rows and (b) the peer
    # controls are UNTOUCHED (exact-email sweep, no collateral).
    with Session(engine) as s:
        from sqlalchemy import func, or_
        from sqlalchemy import select as sa_select

        def _n(model, cond):
            return s.exec(sa_select(func.count()).select_from(model).where(cond)).one()[0]

        # tombstone ownership of a representative spread of the new columns
        assert _n(ContentType, ContentType.created_by == tomb) == 1
        assert _n(KeyChangeLog, KeyChangeLog.actor == tomb) == 1
        assert _n(TermCandidate, TermCandidate.decided_by == tomb) == 1
        assert _n(WorkflowRun, WorkflowRun.created_by == tomb) == 1
        assert (
            _n(Glossary, or_(Glossary.created_by == tomb, Glossary.approved_by == tomb)) == 2
        )  # gl + gl_appr
        assert (
            _n(StyleGuide, or_(StyleGuide.created_by == tomb, StyleGuide.approved_by == tomb)) == 2
        )
        assert _n(Vendor, Vendor.created_by == tomb) == 1
        assert _n(EvalRun, EvalRun.created_by == tomb) == 1
        # peer controls survive intact (must NOT be swept by an exact-email erase)
        peer = "peer@example.com"
        assert _n(ContentType, ContentType.created_by == peer) == 1, "peer ContentType swept"
        assert _n(KeyChangeLog, KeyChangeLog.actor == peer) == 1, "peer KeyChangeLog swept"
        assert _n(TermCandidate, TermCandidate.decided_by == peer) == 1, "peer TermCandidate swept"
        assert _n(WorkflowRun, WorkflowRun.created_by == peer) == 1, (
            "peer WorkflowRun swept (erase over-reached the live-SMTP-target column)"
        )
    # the banner reports the widening sweeps (spot-check the live-send-target label +
    # the two-column Glossary labels)
    assert "workflow_runs" in msg and "vendors" in msg and "glossaries" in msg, (
        "erase banner should report the widening sweeps",
        msg,
    )
    print(
        "  erase: widening columns tombstoned (WorkflowRun live-send target incl.); "
        "peer controls untouched; banner reports the widening sweeps"
    )

    # HIGH-1: the composite KeyChangeLog.actor row is SUBSTRING-swept — its embedded
    # email became the tombstone, the surrounding grammar preserved, and NO raw email
    # remains. The peer composite/bare rows are untouched.
    with Session(engine) as s:
        from sqlalchemy import select as sa_select

        kcls = s.exec(sa_select(KeyChangeLog)).scalars().all()
        composite = [k for k in kcls if k.actor.startswith("workflow:wf7:")]
        assert composite, "composite KeyChangeLog row vanished"
        assert tomb in composite[0].actor and TARGET not in composite[0].actor, (
            "HIGH-1: composite KeyChangeLog.actor not substring-swept to the tombstone",
            composite[0].actor,
        )
        assert all(TARGET not in (k.actor or "") for k in kcls), (
            "HIGH-1: a raw email survived in KeyChangeLog.actor"
        )
    assert "key_change_logs" in msg, "banner should report the KeyChangeLog sweep"
    print(
        "  erase: HIGH-1 composite KeyChangeLog.actor substring-swept "
        "(grammar kept, email tombstoned, no raw leak)"
    )

    # HIGH-3: Customer.settings contact emails (primary/technical + the renamed custom
    # key) are tombstoned; the free-text `notes` MENTION is deliberately left (exact-match
    # sweep, matching the never-scan-free-text posture); the peer contact is untouched.
    with Session(engine) as s:
        from sqlmodel import select as sm_select

        c = s.exec(sm_select(Customer).where(Customer.name == "Cust1")).first()
        assert c is not None, "seeded customer vanished"
        st = c.settings or {}
        assert st["primary_contact"] == tomb and st["technical_contact"] == tomb, (
            "HIGH-3: Customer.settings contacts not tombstoned",
            st,
        )
        assert st["billing_owner_email"] == tomb, (
            "HIGH-3: renamed/custom contact key not swept (value-level pass failed)",
            st,
        )
        assert st["peer_contact"] == "cust-peer@third.example.com", (
            "HIGH-3: peer contact must be untouched (exact-match sweep)"
        )
        assert TARGET in st["notes"], (
            "HIGH-3: a free-text notes MENTION should be left intact (exact-match sweep, "
            "never-scan-free-text posture)",
            st,
        )
    assert "customer_settings" in msg, "banner should report the Customer.settings sweep"
    print(
        "  erase: HIGH-3 Customer.settings contacts tombstoned; free-text mention + "
        "peer contact untouched; banner reports it"
    )

    # =====================================================================
    # L2 (issue #54): a RE-erase of an already-tombstoned user reports ZERO rewrites.
    # The first erase left every attribution column at the tombstone; the count loop
    # used `column == email`, and on re-erase `email` IS the tombstone, so the banner
    # falsely re-reported the first erase's rows (e.g. "1 query_thread_authors") as
    # fresh tomb->tomb no-ops. The fix counts only rows that actually change, so a
    # fully idempotent re-erase names NO rewrites.
    ok2, msg2 = gdpr.erase_user(target.id, actor="admin1@example.com", ip="1.1.1.1")
    assert ok2, msg2
    assert "no linked records" in msg2, (
        f"L2: a re-erase must report no rewrites (fully idempotent), got: {msg2}"
    )
    # And specifically none of the reported-on-first-erase labels reappear with a count.
    for stale in (
        "query_thread_authors",
        "query_thread_assignees",
        "query_post_authors",
        "workflow_step_assignees",
        "workflow_runs",
        "glossaries",
        "vendors",
        "project_members",
    ):
        assert stale not in msg2, (
            f"L2: re-erase banner over-reported {stale!r} as a rewrite: {msg2}"
        )
    # The user row is still the same tombstone (the re-erase changed nothing).
    with Session(engine) as s:
        assert s.get(User, target.id).email == tomb, "re-erase disturbed the tombstone"
    print("  erase: L2 re-erase is fully idempotent -- banner reports no rewrites")

    # =====================================================================
    # POST-ERASE: an erased user can still be exported (tombstone export works)
    # =====================================================================
    secs2 = {}
    for section, rec in gdpr.export_user(target.id):
        secs2.setdefault(section, []).append(rec)
    assert secs2["user"][0]["email"] == tomb, "tombstone export should show the tombstone email"
    # the tombstone still owns the rewritten rows, so those sections are present
    assert "projects" in secs2 and secs2["projects"][0]["created_by"] == tomb
    # no audit rows are attributed to the tombstone (the erase event's actor is the
    # admin, and the subject's history stayed under the original email) -> no leak
    assert "audit" not in secs2, "erased subject must not carry audit rows under the tombstone"
    print("  post-erase: the anonymized user still exports (tombstone identity)")

    # =====================================================================
    # Art. 17 reaching TRANSLATION MEMORY (Phase B chunk 3): user-attribution
    # erasure anonymizes rows, but a full right-to-erasure of a project's content
    # must also reach the project-scoped TM (SegmentTM + FullTextTM), which carry
    # NO user column. retention.delete_project_tm / delete_document are that path.
    # =====================================================================
    from app.models import FullTextTM, Segment, SegmentEmbedding, SegmentTM
    from app.services import retention

    with Session(engine) as s:
        p = Project(
            name="Erase-me TM project", source_locale="en-US", created_by="admin1@example.com"
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        erase_pid = p.id
        doc = Document(
            project_id=erase_pid, filename="secret.txt", fmt="txt", created_by="admin1@example.com"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        erase_did = doc.id
        s.add(
            Segment(
                document_id=erase_did,
                target_locale="fr-FR",
                order_index=0,
                source_text="secret",
                target_text="[fr] secret",
            )
        )
        seg_tm = SegmentTM(
            project_id=erase_pid,
            source_locale="en-US",
            target_locale="fr-FR",
            source_text="secret",
            target_text="[fr] secret",
            source_hash="gh1",
        )
        s.add(seg_tm)
        s.add(
            FullTextTM(
                project_id=erase_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_hash="gh2",
                source_text="secret",
                target_text="[fr] secret",
            )
        )
        s.commit()
        s.refresh(seg_tm)
        # A source-derived vector: erasure must NOT leave it orphaned (GDPR finding).
        s.add(
            SegmentEmbedding(
                segment_tm_id=seg_tm.id,
                project_id=erase_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                model="mock",
                dim=1,
                embedding=b"\x00\x00\x80?",
            )
        )
        s.commit()
    # delete_document reaches TM (cascade) AND removes the doc + its segments.
    with Session(engine) as s:
        counts = retention.delete_document(s, erase_did, cascade_tm=True)
    assert counts["documents"] == 1 and counts["segments"] == 1, counts
    # tm count now includes the SegmentEmbedding row (SegmentTM + FullTextTM + embedding).
    assert counts["tm"] == 3, (
        "Art.17 delete_document must reach SegmentTM+FullTextTM+SegmentEmbedding",
        counts,
    )
    with Session(engine) as s:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        seg_tm_n = s.exec(
            sa_select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == erase_pid)
        ).one()[0]
        ft_tm_n = s.exec(
            sa_select(func.count())
            .select_from(FullTextTM)
            .where(FullTextTM.project_id == erase_pid)
        ).one()[0]
        emb_n = s.exec(
            sa_select(func.count())
            .select_from(SegmentEmbedding)
            .where(SegmentEmbedding.project_id == erase_pid)
        ).one()[0]
        assert seg_tm_n == 0 and ft_tm_n == 0, ("TM survived Art.17 delete", seg_tm_n, ft_tm_n)
        assert emb_n == 0, ("SegmentEmbedding survived Art.17 delete (orphan vector)", emb_n)
        assert s.get(Document, erase_did) is None, "document survived Art.17 delete"
    print("  art17: delete_document(cascade_tm) removes the doc, its segments, TM, AND embeddings")

    # =====================================================================
    # /account/export with auth OFF -> 400 styled error page
    # =====================================================================
    settings.set_many({"security_auth_required": "0"})
    coff = TestClient(app_ref())
    resp = coff.get("/account/export", follow_redirects=False)
    assert resp.status_code == 400, resp.status_code
    assert "Sign in to export your data" in resp.text, "expected the 400 error message"
    print("  route: /account/export with auth OFF -> 400 error page")

    # =====================================================================
    # MED-2: a scoped (caller_org_id) export org-confines the widening sections that
    # carry an org_id. A subject in org 1 who authored CONFIG rows in BOTH org 1 and
    # org 2 must, under an org-1-scoped export, receive ONLY the org-1 rows.
    # =====================================================================
    from app.models import Organization

    MED2 = "med2-subject@example.com"
    med2 = security.create_user(MED2, "Med2 Subject", Role.ADMIN, "med2-pass-1234")
    with Session(engine) as s:
        # Real Organization rows (FK enforcement is ON in the SQLite suite).
        org_a, org_b = Organization(name="Org-A"), Organization(name="Org-B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
        u = s.get(User, med2.id)
        u.org_id = oa
        s.add(u)
        # config rows this subject authored in two different orgs
        s.add(EngineProfile(name="EP-org1", created_by=MED2, org_id=oa))
        s.add(EngineProfile(name="EP-org2", created_by=MED2, org_id=ob))
        s.add(Vendor(name="V-org1", created_by=MED2, org_id=oa))
        s.add(Vendor(name="V-org2", created_by=MED2, org_id=ob))
        s.commit()
    scoped = {}
    for section, rec in gdpr.export_user(med2.id, caller_org_id=oa):
        scoped.setdefault(section, []).append(rec)
    ep_names = {e["name"] for e in scoped.get("engine_profiles", [])}
    v_names = {v["name"] for v in scoped.get("vendors", [])}
    assert ep_names == {"EP-org1"}, (
        "MED-2: org-scoped export leaked a cross-org EngineProfile",
        ep_names,
    )
    assert v_names == {"V-org1"}, ("MED-2: org-scoped export leaked a cross-org Vendor", v_names)
    # unscoped (operator) export still sees BOTH — byte-identical to today
    unscoped = {}
    for section, rec in gdpr.export_user(med2.id):
        unscoped.setdefault(section, []).append(rec)
    assert {e["name"] for e in unscoped["engine_profiles"]} == {"EP-org1", "EP-org2"}, (
        "MED-2: unscoped export must still see both orgs' rows"
    )
    print(
        "  export: MED-2 org-scoped export confines org-stamped config sections "
        "(cross-org EngineProfile/Vendor excluded; unscoped still sees both)"
    )

    # =====================================================================
    # R2: ANCHORED substring matching (the round-2 root cause) across all 4 paths.
    # A peer whose address CONTAINS the subject's as a substring must be byte-untouched
    # by the erase AND never exported for the subject.
    # =====================================================================

    # unit-level: _safe_endpoint emits scheme+host ONLY, re-brackets IPv6, fails closed
    assert (
        gdpr._safe_endpoint("https://svcacct:S3CR3T@vendor.example/intake?apikey=X")
        == "https://vendor.example"
    ), "R2 HIGH-2: endpoint not reduced to scheme+host"
    assert (
        gdpr._safe_endpoint("https://[2001:db8::1]:8443/hooks/tok") == "https://[2001:db8::1]:8443"
    ), "R2 LOW-A: IPv6 host lost its brackets"
    assert gdpr._safe_endpoint("https://host/hooks/SECRETTOKEN") == "https://host", (
        "R2 LOW-B: webhook path not dropped"
    )
    print("  unit: _safe_endpoint -> scheme+host only, IPv6 bracketed, path dropped")

    ANN = "ann@corp.com"
    PEERS = ["joann@corp.com", "leann@corp.com", "ann@corp.com.mx"]  # each CONTAINS ANN
    ann = security.create_user(ANN, "Ann", Role.TRANSLATOR, "ann-pass-123456")
    with Session(engine) as s:
        p = Project(name="anchor-proj", created_by="admin@example.com")
        acst = Customer(name="Anchor-Cust")
        s.add(p)
        s.add(acst)
        s.commit()
        s.refresh(p)
        s.refresh(acst)
        anchor_pid = p.id  # KeyChangeLog.key_id collides with the main flow; key on this
        # item 1 (_scrub_text_column) + item 2 (_iter_by_like confirm): composite
        # KeyChangeLog.actor for the subject + each substring-containing peer.
        s.add(KeyChangeLog(project_id=p.id, key_id=1, actor=f"workflow:wf1:es by {ANN}"))
        for i, pe in enumerate(PEERS):
            s.add(KeyChangeLog(project_id=p.id, key_id=10 + i, actor=f"workflow:wf1:es by {pe}"))
        # item 4 (Job JSON): subject actor + a substring-containing peer in the same blob.
        s.add(
            Job(
                kind="connector_translate",
                status=JobStatus.COMPLETED,
                created_by="scheduler",
                payload={"actor": ANN},
                result={"cc": "joann@corp.com"},
            )
        )
        # item 3 (customers confirm) + narrowing + MED-D leaf mask: subject contact,
        # two peers-as-top-level-values (dropped by narrowing), AND two CONTAINER keys
        # co-holding the subject + a third party (nested dict with a NO-TLD address, and
        # a list) — the third parties must be leaf-masked, not shipped raw.
        ac = s.get(Customer, acst.id)
        ac.settings = {
            "primary_contact": ANN,
            "peer_contact": "joann@corp.com",  # top-level peer -> dropped
            "combined": "ann@corp.com.mx",  # longer address -> not matched
            "nested": {"owner": "dave@nodots", "sub_owner": ANN, "quota": 5},  # MED-D
            "team": [ANN, "x@y.com"],  # MED-D list co-holding
        }
        s.add(ac)
        s.commit()

    # EXPORT before erase: only the subject's rows/keys surface (anchored confirms).
    asecs = {}
    for section, rec in gdpr.export_user(ann.id):
        asecs.setdefault(section, []).append(rec)
    kcl_actors = [k["actor"] for k in asecs.get("key_change_logs", [])]
    assert kcl_actors == [f"workflow:wf1:es by {ANN}"], (
        "R2 item2: _iter_by_like confirm over-matched a longer address",
        kcl_actors,
    )
    acust = asecs.get("customers", [])
    assert len(acust) == 1, ("R2 item3: expected exactly one matched customer", acust)
    a_settings = acust[0]["settings"]
    # narrowing: only the keys whose value anchored-matches the subject survive; the
    # top-level peer keys (peer_contact / combined) are DROPPED entirely.
    assert set(a_settings) == {"[redacted-key]", "[redacted-key]#2", "[redacted-key]#3"}, (
        "R2 item3: customers settings not narrowed to the anchored-matching keys",
        a_settings,
    )
    assert a_settings["[redacted-key]"] == ANN, "flat subject contact must be kept"
    # #380: retain only subject paths, including inside nested containers.
    assert a_settings["[redacted-key]#2"] == {"[redacted-key]": ANN}, (
        "R2 MED-D nested subtree leaked",
        a_settings["[redacted-key]#2"],
    )
    assert a_settings["[redacted-key]#3"] == [ANN], (
        "R2 MED-D list subtree leaked",
        a_settings["[redacted-key]#3"],
    )
    a_blob = json.dumps(a_settings)
    for third in ("dave@nodots", "x@y.com", "joann@corp.com", "ann@corp.com.mx"):
        assert third not in a_blob, ("R2 MED-D/narrowing: a third party leaked", third)
    print(
        "  export: R2 anchored confirms + MED-D leaf mask — customers settings surface "
        "ONLY the subject (top-level peers dropped; nested/list third parties masked)"
    )

    # ERASE the subject, then prove every peer is byte-untouched.
    ok, _ = gdpr.erase_user(ann.id, actor="admin1@example.com")
    assert ok, "anchor subject erase failed"
    ann_tomb = f"deleted-user-{ann.id}@erased.invalid"
    with Session(engine) as s:
        from sqlalchemy import select as sa_select

        kcls = {
            k.key_id: k.actor
            for k in s.exec(sa_select(KeyChangeLog).where(KeyChangeLog.project_id == anchor_pid))
            .scalars()
            .all()
        }
        assert kcls[1] == f"workflow:wf1:es by {ann_tomb}", (
            "R2 item1: subject KeyChangeLog composite not swept",
            kcls.get(1),
        )
        for i, pe in enumerate(PEERS):
            assert kcls[10 + i] == f"workflow:wf1:es by {pe}", (
                "R2 item1 BLOCKER: a substring-containing peer was corrupted by the "
                "unanchored .replace()",
                pe,
                kcls.get(10 + i),
            )
        # item 4: Job JSON — subject actor tombstoned, peer cc untouched
        aj = [
            j
            for j in s.exec(sa_select(Job)).scalars().all()
            if (j.payload or {}).get("actor") in (ANN, ann_tomb)
        ]
        assert aj and aj[0].payload["actor"] == ann_tomb, "R2 item4: Job actor not swept"
        assert aj[0].result["cc"] == "joann@corp.com", (
            "R2 item4: a substring-containing peer corrupted in Job JSON"
        )
        # item 3: settings — subject contact tombstoned, both peers untouched
        acr = s.exec(sa_select(Customer).where(Customer.name == "Anchor-Cust")).scalars().first()
        assert acr.settings["primary_contact"] == ann_tomb, "R2 item3: contact not swept"
        assert (
            acr.settings["peer_contact"] == "joann@corp.com"
            and acr.settings["combined"] == "ann@corp.com.mx"
        ), "R2 item3: a substring-containing peer contact corrupted"
        # nested/list: the subject leaves are tombstoned (exact-match value sweep), the
        # third-party leaves + non-string leaves untouched (erase, not the export mask)
        assert acr.settings["nested"] == {
            "owner": "dave@nodots",
            "sub_owner": ann_tomb,
            "quota": 5,
        }, ("R2 item3: nested subject leaf not swept / peer corrupted", acr.settings["nested"])
        assert acr.settings["team"] == [ann_tomb, "x@y.com"], (
            "R2 item3: list subject leaf not swept / peer corrupted",
            acr.settings["team"],
        )
    # whole-DB scan is clean for the subject (no raw ANN anywhere)
    assert not _no_email_anywhere(ANN), "R2: raw subject email survived erase somewhere"
    print(
        "  erase: R2 anchored sweeps — subject swept across KeyChangeLog/Job-JSON/"
        "settings; all 3 substring-containing peers byte-untouched; whole-DB scan clean"
    )

    test_223_job_walk_anchored()
    test_gate1_named_job_masking()
    test_gate1_json_content_sweep()
    test_277_post_survey_columns()
    test_277_identity_column_ratchet()
    test_270_account_security_metadata()

    print("GDPR OK")


# =========================================================================================
# #223: the export's Job JSON walk matched the subject's email as a plain SUBSTRING, so
# ann@x.test's export (self-service included, any role) shipped joann@x.test's whole Job
# rows. The walk must match only the WHOLE address, and a job the subject is named in but
# did not create must not carry anyone else's email.
# =========================================================================================
def test_223_job_walk_anchored() -> None:
    import json

    ann_email, joann_email = "ann@x223.test", "joann@x223.test"
    pw = "ann223-pass-123456"  # pregate: allow-secret throwaway suite user
    ann = security.create_user(ann_email, "Ann", Role.VIEWER, pw)
    security.create_user(joann_email, "Joann", Role.MANAGER, "joann223-pass-123456")
    with Session(engine) as s:
        private_project = Project(name="Private #380", created_by=joann_email)
        s.add(private_project)
        s.commit()
        s.refresh(private_project)
        private_project_id = private_project.id
        # Joann's own connector job: names only joann@ (a LONGER address containing ann@).
        s.add(
            Job(
                kind="connector_translate",
                status=JobStatus.COMPLETED,
                created_by=joann_email,
                payload={"connection_id": 7, "actor": joann_email},
                result={"note": "JOANN223-PRIVATE item 'Disciplinary hearing'"},
            )
        )
        # A job someone else created that DOES name Ann as a whole address, next to a
        # third party. Ann is entitled to her occurrence, never the third party's address.
        s.add(
            Job(
                kind="connector_translate",
                status=JobStatus.COMPLETED,
                created_by="owner223@third.test",
                message="PRIVATE_MESSAGE_380",
                error="PRIVATE_ERROR_380",
                payload={
                    "project_id": private_project_id,
                    "actor": ann_email,
                    "cc": ["carol223@third.test"],
                    "private": "PRIVATE_PAYLOAD_380",
                },
                result={
                    "note": f"reviewed by {ann_email} and dave223@third.test; PRIVATE_NOTE_380",
                    "salary": 987654321,
                    "confidential": "OTHER_PROJECT_CONFIDENTIAL_RESULT",
                },
            )
        )
        s.commit()

    jobs = [r for sec, r in gdpr.export_user(ann.id) if sec == "jobs"]
    blob = json.dumps(jobs)
    assert "JOANN223-PRIVATE" not in blob and joann_email not in blob, (
        "#223: a colleague whose address CONTAINS the subject's leaked into the export",
        blob,
    )
    assert len(jobs) == 1, ("#223: expected exactly the one job naming the subject", jobs)
    named = jobs[0]
    assert named == {
        "id": named["id"],
        "subject": ann_email,
        "relationship": "mentioned",
        "fields": ["payload", "result"],
    }, ("#380: named-only jobs must contain only subject-specific metadata", named)
    for third in ("owner223@third.test", "carol223@third.test", "dave223@third.test"):
        assert third not in json.dumps(named), ("#223: third-party email shipped", third, named)
    print("  #223: export Job walk anchored (joann@ not in ann@'s copy); third parties masked")

    # Route level: the self-service export by a viewer, auth ON.
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    try:
        c = TestClient(app_ref())
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={"email": ann_email, "password": pw, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        r = c.get("/account/export")
        assert r.status_code == 200, r.status_code
        exported_jobs = [
            row
            for line in r.text.splitlines()
            if (row := json.loads(line)).get("section") == "jobs"
        ]
        assert exported_jobs == [{"section": "jobs", **named}], exported_jobs
        # Customer confinement must not turn a mention into permission either.
        from app.services import access

        with Session(engine) as s:
            assert not access.can_view_project(s, ann, private_project_id)
            customer = Customer(name="Ann's customer #380")
            other_customer = Customer(name="Private customer #380")
            s.add_all([customer, other_customer])
            s.flush()
            subject = s.get(User, ann.id)
            subject.customer_id = customer.id
            project = s.get(Project, private_project_id)
            project.customer_id = other_customer.id
            s.add_all([subject, project])
            s.commit()
            assert not access.can_view_project(s, subject, project)
        r = c.get("/account/export")
        assert r.status_code == 200
        customer_jobs = [
            row
            for line in r.text.splitlines()
            if (row := json.loads(line)).get("section") == "jobs"
        ]
        assert customer_jobs == [{"section": "jobs", **named}]
        assert "JOANN223-PRIVATE" not in r.text and joann_email not in r.text, (
            "#223: GET /account/export by a viewer shipped a colleague's job"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
    print("  #223: GET /account/export (viewer) carries no colleague job")


# =========================================================================================
# Gate fix-gdpr round 1. M1: masking in a job the subject is only named in must not depend
# on address grammar, and must cover dict keys. L1: the subject's own o'brien-style address
# is never mangled. L2: a sentence-final period still matches (export AND erase). L3: case
# variants and non-ASCII subjects inside Job JSON are found and erased.
# =========================================================================================
def test_gate1_named_job_masking() -> None:
    import json

    def _job(**kw):
        with Session(engine) as s:
            j = Job(kind="connector_translate", status=JobStatus.COMPLETED, **kw)
            s.add(j)
            s.commit()
            s.refresh(j)
            return j.id

    def _jobs(uid):
        return [r for sec, r in gdpr.export_user(uid) if sec == "jobs"]

    subj = "ann@g1.test"
    u = security.create_user(subj, "A", Role.VIEWER, "g1-ann-pass-1234567")
    thirds = [
        "bob@localhost",
        "josé@third.test",
        "carl@exämple.com",
        "dave@[10.0.0.1]",
        "gina@host.123",
        "FRANK@THIRD.TEST",
        '"carol smith"@third.test',
    ]
    jid = _job(
        created_by="owner@g1third.test",
        payload={"actor": subj, "others": thirds, "erin@third.test": "as-key"},
        result={"note": f"reviewed by {subj}, cc bob@localhost."},
    )
    recs = [r for r in _jobs(u.id) if r["id"] == jid]
    assert len(recs) == 1, recs
    blob = json.dumps(recs, ensure_ascii=False)
    for third in [*thirds, "erin@third.test", "owner@g1third.test"]:
        assert third not in blob, ("gate M1: third-party address shipped", third, blob)
    assert recs[0]["subject"] == subj and recs[0]["fields"] == ["payload", "result"], recs
    print("  gate M1: named-job masking by @ token (no-dot/IDN/IP/numeric/key) holds")

    # #380: neither matching keys nor surrounding text/numeric leaves are safe to copy.
    for field in ("payload", "result"):
        raw = {f"PRIVATE_KEY_380 {subj}": [{"salary": 987654321, "note": "PRIVATE_380"}]}
        mention_id = _job(created_by="owner@g1third.test", **{field: raw})
        assert [r for r in _jobs(u.id) if r["id"] == mention_id] == [
            {
                "id": mention_id,
                "subject": subj,
                "relationship": "mentioned",
                "fields": [field],
            }
        ]
        with Session(engine) as s:
            assert getattr(s.get(Job, mention_id), field) == raw, "export mutated stored JSON"

    # L1: an o'brien subject keeps its own address intact; a longer address stays out.
    ob = "o'brien@g1.test"
    ou = security.create_user(ob, "O", Role.VIEWER, "g1-obrien-pass-12345")
    jid_ob = _job(created_by="owner@g1third.test", payload={"actor": ob}, result={"n": f"hi {ob}"})
    got = [r for r in _jobs(ou.id) if r["id"] == jid_ob]
    assert got and got[0]["subject"] == ob and got[0]["fields"] == ["payload", "result"], got
    print("  gate L1: an o'brien-style subject address is exported unmangled")

    # L2: a sentence-final period.
    pete = security.create_user("pete@g1.test", "P", Role.VIEWER, "g1-pete-pass-1234567")
    jid_p = _job(created_by="owner@g1third.test", result={"note": "Assigned to pete@g1.test."})
    assert any(r["id"] == jid_p for r in _jobs(pete.id)), "gate L2: trailing period not matched"
    # L3: case variant and a non-ASCII subject (stored JSON-escaped).
    zoe = security.create_user("zoe@g1.test", "Z", Role.VIEWER, "g1-zoe-pass-12345678")
    jid_z = _job(created_by="owner@g1third.test", payload={"actor": "Zoe@G1.test"})
    assert any(r["id"] == jid_z for r in _jobs(zoe.id)), "gate L3: case variant not matched"
    mu_email = "müller@g1.test"
    mu = security.create_user(mu_email, "M", Role.VIEWER, "g1-mueller-pass-12345")
    jid_m = _job(created_by="owner@g1third.test", payload={"actor": mu_email})
    assert any(r["id"] == jid_m for r in _jobs(mu.id)), "gate L3: non-ASCII subject not found"
    print("  gate L2/L3: trailing period, case variant and non-ASCII subject are exported")

    for uid in (pete.id, zoe.id, mu.id):
        ok, msg = gdpr.erase_user(uid, actor="admin1@example.com")
        assert ok, msg
    with Session(engine) as s:
        for jid_x, uid, raw in (
            (jid_p, pete.id, "pete@g1.test"),
            (jid_z, zoe.id, "zoe@g1.test"),
            (jid_m, mu.id, mu_email),
        ):
            j = s.get(Job, jid_x)
            text = json.dumps([j.payload, j.result], ensure_ascii=False).lower()
            assert raw.lower() not in text, ("gate L2/L3: address survived erase", raw, text)
            assert f"deleted-user-{uid}@erased.invalid" in text, text
    print("  gate L2/L3: erase rewrites those three occurrences to the tombstone")


# =========================================================================================
# Gate fix-gdpr round 1 (M2): JSON columns that carry an address by content are swept by
# erase: the job-queue ledger copy of Job.payload, and dataset item meta rules_labeler.
# =========================================================================================
def test_gate1_json_content_sweep() -> None:
    import json

    from app.models import EvalDatasetItem, JobQueue

    quinn = "quinn@g1.test"
    u = security.create_user(quinn, "Q", Role.MANAGER, "g1-quinn-pass-1234567")
    with Session(engine) as s:
        j = Job(kind="connector_translate", created_by=quinn, payload={"actor": quinn})
        ds = EvalDataset(name="DSg1", created_by="admin@example.com")
        s.add(j)
        s.add(ds)
        s.commit()
        s.refresh(j)
        s.refresh(ds)
        s.add(JobQueue(job_id=j.id, kind=j.kind, payload={"connection_id": 1, "actor": quinn}))
        s.add(JobQueue(job_id=j.id, kind=j.kind, payload={"actor": "peer@g1.test"}))
        item = EvalDatasetItem(
            dataset_id=ds.id, meta={"rules_labeler": quinn, "rules_target_locale": "de-DE"}
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = item.id
    labels = [r for sec, r in gdpr.export_user(u.id) if sec == "dataset_item_labels"]
    assert labels and labels[0]["meta"] == {"[redacted-key]": quinn}, labels
    ok, msg = gdpr.erase_user(u.id, actor="admin1@example.com")
    assert ok, msg
    tomb = f"deleted-user-{u.id}@erased.invalid"
    with Session(engine) as s:
        from sqlalchemy import select as sa_select

        qs = s.exec(sa_select(JobQueue)).scalars().all()
        blob = json.dumps([q.payload for q in qs])
        assert quinn not in blob, ("gate M2: JobQueue.payload kept the address", blob)
        assert tomb in blob and "peer@g1.test" in blob, blob
        assert s.get(EvalDatasetItem, item_id).meta["rules_labeler"] == tomb
    assert "job_queue_payloads" in msg and "dataset_item_meta" in msg, msg
    print("  gate M2: erase sweeps JobQueue.payload and dataset item meta; peers untouched")


# =========================================================================================
# #277: attribution columns added after the 2026-08-17 survey kept the subject's email
# after erase. Each is swept AND exported now; peers stay untouched.
# =========================================================================================
def test_277_post_survey_columns() -> None:
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.models import (
        AdvancedMission,
        AdvancedRun,
        Connection,
        DataExportRun,
        EvalDatasetItem,
        GlossaryConcept,
        ProjectAutomation,
        ProjectBudget,
        QualityGoldItem,
        QualityGoldLabel,
        QualityLens,
        QualityLensExample,
        UserGroup,
        UserGroupMember,
    )

    subj_email, peer = "subject277@x.test", "peer277@x.test"
    subj = security.create_user(subj_email, "S", Role.MANAGER, "subject277-pass-12345")
    peer_u = security.create_user(peer, "P", Role.MANAGER, "peer277-pass-1234567")
    with Session(engine) as s:
        proj = Project(name="P277", created_by="admin@example.com")
        gl = Glossary(name="G277", created_by="admin@example.com")
        grp = UserGroup(name="grp277")
        ds = EvalDataset(name="DS277", created_by="admin@example.com")
        for row in (proj, gl, grp, ds):
            s.add(row)
        s.commit()
        for row in (proj, gl, grp, ds):
            s.refresh(row)
        lens = QualityLens(slug="l277", created_by=subj_email)
        mission = AdvancedMission(project_id=proj.id, name="m277", created_by=subj_email)
        item = EvalDatasetItem(dataset_id=ds.id)
        for row in (lens, mission, item):
            s.add(row)
        s.commit()
        for row in (lens, mission, item):
            s.refresh(row)
        gold = QualityGoldItem(dataset_item_id=item.id, labeler=subj_email)
        conn277 = Connection(kind="zendesk", name="C277", config={})
        s.add(gold)
        s.add(conn277)
        s.commit()
        s.refresh(gold)
        s.refresh(conn277)
        rows = [
            DataExportRun(run_uuid="dx277", actor=subj_email),
            DataExportRun(run_uuid="dx277p", actor=peer),
            ProjectBudget(project_id=proj.id, created_by=subj_email),
            ProjectAutomation(
                project_id=proj.id,
                connection_id=conn277.id,
                run_connection_id=conn277.id,
                created_by=subj_email,
            ),
            QualityLens(slug="l277p", created_by=peer),
            QualityLensExample(lens_id=lens.id, created_by=subj_email),
            AdvancedRun(mission_id=mission.id, project_id=proj.id, created_by=subj_email),
            GlossaryConcept(glossary_id=gl.id, created_by=subj_email),
            GlossaryConcept(glossary_id=gl.id, created_by=peer),
            UserGroupMember(group_id=grp.id, user_id=peer_u.id, added_by=subj_email),
            UserGroupMember(group_id=grp.id, user_id=subj.id, added_by="scim"),
            QualityGoldLabel(gold_item_id=gold.id, labeler=subj_email, note="n277"),
        ]
        for row in rows:
            s.add(row)
        s.commit()

    new_sections = {
        "data_export_runs": "actor",
        "project_budgets": "created_by",
        "project_automations": "created_by",
        "quality_lenses": "created_by",
        "quality_lens_examples": "created_by",
        "advanced_missions": "created_by",
        "advanced_runs": "created_by",
        "glossary_concepts": "created_by",
        "user_group_members": "added_by",
        "quality_gold_items": "labeler",
        "quality_gold_labels": "labeler",
    }
    got = {}
    for sec, rec in gdpr.export_user(subj.id):
        got.setdefault(sec, []).append(rec)
    for sec, col in new_sections.items():
        recs = got.get(sec, [])
        assert recs and any(r[col] == subj_email for r in recs), ("#277 export missing", sec)
        assert all(peer not in str(r) for r in recs), ("#277 export leaked the peer", sec)
    assert any(m["user_id"] == subj.id for m in got["user_group_members"]), (
        "#277: the subject's own group membership must export"
    )
    print("  #277: the 11 post-survey attribution sections export the subject's rows")

    ok, msg = gdpr.erase_user(subj.id, actor="admin1@example.com")
    assert ok, msg
    tomb = f"deleted-user-{subj.id}@erased.invalid"
    checks = [
        (DataExportRun, DataExportRun.actor),
        (ProjectBudget, ProjectBudget.created_by),
        (ProjectAutomation, ProjectAutomation.created_by),
        (QualityLens, QualityLens.created_by),
        (QualityLensExample, QualityLensExample.created_by),
        (AdvancedMission, AdvancedMission.created_by),
        (AdvancedRun, AdvancedRun.created_by),
        (GlossaryConcept, GlossaryConcept.created_by),
        (UserGroupMember, UserGroupMember.added_by),
        (QualityGoldItem, QualityGoldItem.labeler),
        (QualityGoldLabel, QualityGoldLabel.labeler),
    ]
    with Session(engine) as s:

        def _n(model, cond):
            return s.exec(sa_select(func.count()).select_from(model).where(cond)).one()[0]

        for model, col in checks:
            left = _n(model, col == subj_email)
            assert left == 0, ("#277: email survived erase", model.__name__, col.key, left)
            assert _n(model, col == tomb) >= 1, ("#277: not tombstoned", model.__name__, col.key)
        assert _n(DataExportRun, DataExportRun.actor == peer) == 1, "peer DataExportRun swept"
        assert _n(QualityLens, QualityLens.created_by == peer) == 1, "peer QualityLens swept"
        assert _n(GlossaryConcept, GlossaryConcept.created_by == peer) == 1, "peer concept swept"
    for label in ("data_export_runs", "glossary_concepts", "user_group_member_adders"):
        assert label in msg, ("#277: erase banner should report", label, msg)
    print("  #277: erase tombstones all 11 post-survey columns; peer rows untouched")


# =========================================================================================
# #277 ratchet. THE RULE: a column "can hold a user identity" when it is a string or JSON
# column whose name is email / actor / author / owner / initiator / assignee / labeler /
# requester / reviewer / approver / uploader (alone or as a `_`-separated part, so
# assignee_email and actor_kind qualify), or ends in `_by`, or contains `email`. Every such
# column in SQLModel.metadata must be swept by erase (_ERASE_SWEEP or a dedicated erase_user
# block listed in _IDENTITY_SWEPT_ELSEWHERE) or sit on _IDENTITY_NOT_SWEPT with a reason.
# Integer user_id FKs are out of scope (they carry no address; the User row is scrubbed).
# =========================================================================================
_IDENTITY_NAME = re.compile(
    r"(^|_)(email|actor|author|owner|initiator|assignee|labeler|requester|reviewer|approver"
    r"|uploader)(_|$)|_by$|email"
)


def _textish(col_type) -> bool:
    """A column that can hold a string: String/Text/AutoString (a TypeDecorator over
    String) or JSON (a string leaf)."""
    import sqlalchemy as sa

    if isinstance(col_type, sa.types.TypeDecorator):
        col_type = col_type.impl_instance
    return isinstance(col_type, (sa.String, sa.Text, sa.JSON))


def test_277_identity_column_ratchet() -> None:
    from sqlmodel import SQLModel

    import app.models  # noqa: F401  (registers every table)

    candidates = set()
    for table in SQLModel.metadata.tables.values():
        for col in table.columns:
            if _textish(col.type) and _IDENTITY_NAME.search(col.name):
                candidates.add(f"{table.name}.{col.name}")
    # Gate round 1 (M2): EVERY JSON column, whatever its name, needs a decision: swept by
    # content (_JSON_CONTENT_SWEEP), handled by a dedicated block, or allowlisted.
    import sqlalchemy as sa

    json_cols = set()
    for table in SQLModel.metadata.tables.values():
        for col in table.columns:
            t = col.type
            if isinstance(t, sa.types.TypeDecorator):
                t = t.impl_instance
            if isinstance(t, sa.JSON):
                json_cols.add(f"{table.name}.{col.name}")
    swept = {f"{m.__tablename__}.{c.key}" for _l, m, c in gdpr._ERASE_SWEEP}
    elsewhere = dict(getattr(gdpr, "_IDENTITY_SWEPT_ELSEWHERE", {}))
    allow = dict(getattr(gdpr, "_IDENTITY_NOT_SWEPT", {}))
    undecided = sorted(candidates - swept - set(elsewhere) - set(allow))
    assert not undecided, (
        "#277 ratchet: identity column(s) neither swept by gdpr.erase_user nor on "
        "gdpr_common._IDENTITY_NOT_SWEPT with a reason",
        undecided,
    )
    for name, reason in {**elsewhere, **allow}.items():
        assert reason.strip(), f"#277 ratchet: {name} has no reason"
        assert name in candidates | json_cols, ("#277 ratchet: stale entry (no such column)", name)
    assert not (set(allow) & (swept | set(elsewhere))), "a column is both swept and allowlisted"
    assert len(candidates) >= 60, ("#277 ratchet: rule matched implausibly few", len(candidates))

    content = {
        f"{m.__tablename__}.{c.key}" for _l, m, cols, _e in gdpr._JSON_CONTENT_SWEEP for c in cols
    }
    json_allow = dict(getattr(gdpr, "_JSON_NO_IDENTITY", {}))
    json_undecided = sorted(json_cols - content - set(elsewhere) - set(json_allow) - set(allow))
    assert not json_undecided, (
        "gate M2 ratchet: JSON column(s) with no decision (sweep by content, dedicated "
        "block, or _JSON_NO_IDENTITY with a reason)",
        json_undecided,
    )
    for name, reason in json_allow.items():
        assert reason.strip() and name in json_cols, ("stale or reasonless JSON entry", name)
    assert not (set(json_allow) & content), "a JSON column is both swept and allowlisted"
    for label, _m, _c, export in gdpr._JSON_CONTENT_SWEEP:
        assert export.strip() and (
            (export.startswith("exempt:") and len(export) > 12) or " " not in export
        ), ("content sweep entry needs an export section or an exempt reason", label)
    assert {"job.payload", "job.result", "jobqueue.payload"} <= content, content
    print(
        f"  gate M2 ratchet: {len(json_cols)} JSON columns, {len(content)} swept by content, "
        f"{len(json_allow)} allowlisted with reasons"
    )
    print(
        f"  #277 ratchet: {len(candidates)} identity columns, {len(swept)} swept, "
        f"{len(elsewhere)} swept by dedicated blocks, {len(allow)} allowlisted with reasons"
    )


# =========================================================================================
# #270 (r1-data-06): the export carries the password-policy and two-step sign-in METADATA
# about the subject, never the credential material.
# =========================================================================================
def test_270_account_security_metadata() -> None:
    import json
    from datetime import datetime

    from app.models import UserMfa

    u = security.create_user("mfa270@x.test", "M", Role.TRANSLATOR, "mfa270-pass-1234567")
    marker = "MFA270-SECRET-CIPHERTEXT"  # pregate: allow-secret fake ciphertext marker
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.password_must_change = True
        row.password_changed_at = datetime(2026, 9, 1, 12, 0, 0)
        s.add(row)
        s.add(
            UserMfa(
                user_id=u.id,
                secret_enc=marker,
                enabled_at=datetime(2026, 9, 2, 8, 0, 0),
                fail_count=2,
                last_step=123456,
            )
        )
        s.commit()
    recs = [r for sec, r in gdpr.export_user(u.id) if sec == "account_security"]
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["password_must_change"] is True and rec["password_changed_at"].startswith(
        "2026-09-01"
    ), rec
    assert rec["mfa_enabled"] is True and rec["mfa_enabled_at"].startswith("2026-09-02"), rec
    assert rec["mfa_fail_count"] == 2, rec
    blob = json.dumps(list(gdpr.export_user(u.id)), default=str)
    assert marker not in blob and "secret_enc" not in blob and "last_step" not in blob, (
        "#270: MFA credential material leaked into the export"
    )
    print("  #270: export carries password + MFA metadata, never the MFA secret")


def test_380_mention_projections() -> None:
    import copy
    import json

    from app.models import EvalDatasetItem

    email = "ann@probe.test"
    subject = security.create_user(email, "Ann", Role.VIEWER, "probe380-pass-123456")
    shapes = [
        {"contacts": {email: {"salary": 111222333}, "bob@other.test": {"salary": 444555666}}},
        {"rules_labeler": {email: 1, "carol@other.test": 987987}},
        {
            "private-key-bob@other.test": [
                {email.upper(): True},
                {"private-path": email},
                {f"contact {email}": None},
                "carol@other.test",
                987987,
                False,
            ],
            "another-private-key": email,
            email: 111222333,
            "substring": "joann@probe.test",
        },
    ]
    original = copy.deepcopy(shapes)
    ids = []
    with Session(engine) as s:
        ds = EvalDataset(name="Foreign dataset", created_by="manager@other.test")
        s.add(ds)
        s.commit()
        s.refresh(ds)
        for shape in shapes:
            customer = Customer(name="Foreign customer", settings=shape)
            item = EvalDatasetItem(dataset_id=ds.id, meta=shape, source_text="PRIVATE SOURCE")
            s.add(customer)
            s.add(item)
            s.commit()
            s.refresh(customer)
            s.refresh(item)
            ids.append((customer.id, item.id))

    exported = list(gdpr.export_user(subject.id))
    for section, record in exported:
        if section == "customers":
            assert "name" not in record, "#380: foreign customer name disclosed"
    failures = []
    for section, field in (("customers", "settings"), ("dataset_item_labels", "meta")):
        records = [rec[field] for sec, rec in exported if sec == section]
        assert len(records) == len(shapes), (section, records)
        for record in records:
            blob = json.dumps(record).lower()
            assert email in blob, ("#380: subject occurrence missing", section, record)
            for private in (
                "bob@other.test",
                "carol@other.test",
                "111222333",
                "444555666",
                "987987",
                "private",
                "salary",
                "joann@probe.test",
                "true",
                "false",
                "null",
            ):
                if private in blob:
                    failures.append((section, private))
        # Sibling paths must survive key anonymization without collisions.
        assert len(records[2]) == 3, ("#380: mention paths lost", section, records[2])
    assert not failures, ("#380: mention projection leaked unrelated data", failures)
    assert shapes == original, "#380: projection mutated its input"
    with Session(engine) as s:
        for (cid, iid), shape in zip(ids, original, strict=True):
            assert s.get(Customer, cid).settings == shape
            assert s.get(EvalDatasetItem, iid).meta == shape
    print("  #380: customer and dataset mentions exclude private keys and non-email values")


def app_ref():
    # import lazily so os.environ is set first (mirrors the other suites)
    from app.main import app

    return app


if __name__ == "__main__":
    main()
