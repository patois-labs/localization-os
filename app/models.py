"""The whole Localization OS data model, defined once up front.

Later chunks (TM, glossary, workflows, LQA) just *use* these tables, so we never
have to migrate. SQLite + SQLModel; JSON-ish fields use SQLAlchemy JSON columns.

Naming mirrors how real TMS tools think:
  Customer -> Project -> (TargetLocale, Document -> Segment)
  TM:        SegmentTM + FullTextTM
  Glossary:  GlossaryEntry + GlossarySense
  Workflow:  Workflow -> WorkflowStep ; WorkflowRun -> StepRun
  Library:   PromptTemplate
  Quality:   LQAResult

Documented in: docs/architecture/data-and-migrations.md
"""

from datetime import datetime

from sqlalchemy import JSON, Column, Index, LargeBinary, UniqueConstraint, text
from sqlmodel import Field, SQLModel


class LoginThrottle(SQLModel, table=True):
    """Persisted rolling password attempts and bounded admission buckets (#222)."""

    key: str = Field(primary_key=True)
    version: int = 0
    attempts: str = "[]"
    notification: str = ""
    canonical_name: str = Field(default="", index=True)
    locked_until: float = 0
    level: int = 0
    expires_at: float = Field(default=0, index=True)


# --- string "enums" (kept as plain strings for SQLite friendliness) ---
class TransMode:
    SEGMENTED = "segmented"
    FULL_TEXT = "full_text"


class ProviderMode:
    """How a project selects its MT/AI provider per language pair (Phase 2).

    UNIFORM  — one provider (Project.default_provider) for every target pair; the
               pre-Phase-2 default, byte-identical behavior.
    PER_PAIR — each (source, target) pair may name its own provider via a
               ProjectPairProvider row; unmapped pairs fall back to the default.
    """

    UNIFORM = "uniform"
    PER_PAIR = "per_pair"
    ALL = (UNIFORM, PER_PAIR)


class ProjectType:
    """What KIND of content a project holds (SPEC-key-based-projects section 2.1).

    DOCUMENT  — today's document-driven project: files are uploaded, parsed into a
                skeleton + Segments, translated, and rebuilt. The default, and the
                value every pre-Batch-4 project backfills to, so a project that
                never opts in behaves byte-identically.
    KEY_BASED — a Lokalise-style continuous-localization project: the unit of work
                is a StringKey (an identity), its values live as Segments under
                system-managed internal Documents (fmt="keys"), and the deliverable
                is ONE key-value file per locale across the whole project.

    IMMUTABLE after creation in v1 (no type conversion — the service refuses).
    """

    DOCUMENT = "document"
    KEY_BASED = "key_based"
    ALL = (DOCUMENT, KEY_BASED)


class ProjectIntake:
    """Where a project's content COMES FROM (SPEC-connected-projects section 4).

    UPLOAD    - people upload files or push keys (every project before 2026-10-13).
    CONNECTED - content is pulled from a connected system by project automation; uploads
                are hidden on the page and refused by the upload routes.

    Separate from ProjectType, which stays the content SHAPE and picks the editor. Stored, not
    derived from a ProjectAutomation binding, because a connected project exists (and shows
    its setup menu) before any connection is bound; unbinding keeps it connected.
    """

    UPLOAD = "upload"
    CONNECTED = "connected"
    ALL = (UPLOAD, CONNECTED)


class KeyOp:
    """KeyChangeLog.op — the Delta API's verbs. `DELETED` is a first-class ROW, not
    an absence: that is the whole point of the change feed (research 01 section 6)."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    ALL = (CREATED, UPDATED, DELETED)


class KeyEntity:
    """KeyChangeLog.entity — whether a change describes the key itself (metadata,
    creation, soft delete) or ONE of its per-locale/per-plural-form values."""

    KEY = "key"
    VALUE = "value"
    ALL = (KEY, VALUE)


class DocStatus:
    NEW = "new"


class SegStatus:
    NEW = "new"
    MT = "mt"
    EDITED = "edited"
    APPROVED = "approved"
    # The universal SOFT QA-gate routing status (SPEC-wave3-collaboration §2.8/§10 —
    # text-truth). A segment lands here (not MT) so the CAT editor's status chip + the
    # QE badge (Segment.qa_flags) surface it as review-needed. Distinct from
    # WorkflowStep.needs_review (a step-config flag for LLM-adapted prompts) — this is a
    # SEGMENT lifecycle state.
    #
    # WRITTEN by `quality.commit_mt_output`'s soft gates (DNT, glossary adherence,
    # refusal, tag damage, number integrity, placeholder, whitespace, spacing, length
    # runaway, scaffold echo, language mismatch) and the orchestrator's same-family
    # re-assertion — 35 write sites across 9 modules (measured on the tree; the counting
    # rule is `status = ...NEEDS_REVIEW` assignments under app/, excluding this comment).
    # CLEARED only by a human
    # edit; nothing else clears it, so on an established install this status FLOODS
    # (every soft gate that ever fired leaves it behind) — which is exactly why the Wave
    # 3 worklist keys its quality section on live findings, NOT on this status (§8.2).
    # (The earlier claim that only WorkflowStep.qe_gate reaches it was false on both
    # halves: the QE gate routes through commit_mt_output like every other gate, and
    # `qe.py` never writes this constant at all.)
    NEEDS_REVIEW = "needs_review"


class StepType:
    LLM_AGENT = "llm_agent"
    LQA = "lqa"
    HUMAN_REVIEW = "human_review"
    # PROPOSAL-tm-matching-workflow-step (§7): an ORCHESTRATION-ONLY step (D8) that
    # fills qualifying 100% TM matches into the run's rolling text and LOCKS them, so
    # every later step (MT, LQA, fix rounds) skips them. Runs no provider/engine call
    # and carries no prompt -- its whole config is which attached TMs participate
    # (v1: all, in precedence order). See app/services/tm_lock.py.
    TM_MATCH = "tm_match"
    # PROPOSAL-external-processing-step (§9 decision record, combined build Wave A):
    # a dispatch-and-wait step that packages the current translation state, computes a
    # per-vendor scoping estimate, sends the JSON contract out through the SSRF guard to
    # a selected VENDOR, and parks the leg PAUSED (reusing the human_review pause
    # machinery) awaiting a validated return (Wave B). Orchestration-only (D8): it never
    # transforms content. Its config is `WorkflowStep.vendor_id` (validated fail-closed
    # against the document's customer's linked vendors) + `external_wait_hours`. See
    # app/services/vendor_dispatch.py and the EXTERNAL branch in _execute_step.
    EXTERNAL = "external"

    # The canonical whitelist of authorable step types. The shared add_step/update_step
    # seam validates against this so /api + web reject an unknown step_type with a clean
    # error instead of persisting it and running it as an llm_agent (BUG-A201 — the CLI
    # argparse `choices=` was the ONLY surface enforcing it). Keep in sync with the
    # orchestrator's dispatch and the builders' step-type option lists.
    ALL = (LLM_AGENT, LQA, HUMAN_REVIEW, TM_MATCH, EXTERNAL)


class RunStatus:
    PENDING = "pending"
    # A fan-out launch this install had no free run slot for YET (gate A round 3,
    # HIGH-1/2/3). DURABLE and VISIBLE: the row exists from the moment the operator
    # asks, holds NO capacity slot and NO pooled connection, and the process-wide
    # admission scheduler (orchestrator.ADMISSION) flips it to RUNNING as slots free.
    # Distinct from PENDING, which is the momentary pre-execution state a launch passes
    # through on its own thread and which the boot healer FAILS as an orphan: a QUEUED
    # row is exactly the row that SHOULD survive a restart, and the boot reconcile
    # re-adopts it instead. NON-TERMINAL and in-flight for every purpose (authoring
    # guards, the double-submit guard, the batch rollup, cancel), so it counts as work
    # over its (workflow, document, locale) and an operator can cancel it from any
    # surface. `status` is a plain string column (no DB-constrained enum), so the value
    # itself needs no migration; the fan-out grouping column `queue_group` does.
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"  # waiting on a human-review step
    COMPLETED = "completed"
    FAILED = "failed"
    # Operator-driven abort (AUDIT-fulltest-2026-08-08 BUG-A205 / run-cancel train). A
    # TERMINAL status a run enters ONLY via an explicit cancel action (orchestrator.
    # cancel_run) from PENDING/RUNNING/PAUSED. Distinct from FAILED (which means "the
    # work tried and broke") so the timeline reads honestly: a cancelled run stopped
    # because a person stopped it, not because a step errored. A CANCELLED run is
    # terminal and NON-RESUMABLE (a fresh run may be launched); it does NOT count as an
    # in-flight run (workflows._ACTIVE_RUN_STATUSES), so cancelling a stuck PAUSED run is
    # the escape hatch that unblocks re-authoring — the "cancel them, then move/edit/
    # delete" the mid-flight authoring guards have always pointed at. Terminal for the
    # vendor-leg seam too (vendor_dispatch._TERMINAL_RUN_STATUSES): its open legs are
    # closed CANCELLED and a late vendor return is dropped loudly. `status` is a plain
    # string column (no DB-constrained enum), so this needs NO migration. Never written
    # to a StepRun by the state machine — the run-level abort; a run's PAUSED StepRun is
    # flipped to CANCELLED by cancel_run so no stale 'paused' row survives.
    CANCELLED = "cancelled"
    # Conditional-workflow (SPEC-conditional-workflows §3.4, Phase 2): a STEP whose
    # run_condition guard evaluated FALSE was not executed. A StepRun-only status (a
    # RUN is never SKIPPED); distinct from COMPLETED/FAILED so the timeline shows the
    # skip honestly. Plain (non-conditional) runs never produce a SKIPPED row.
    SKIPPED = "skipped"


class ProjectStatus:
    """First-class project lifecycle (P1, items 2+3). The schema ships all seven
    values (Smartling Draft→…→Completed→Closed convergence); P1 wires transitions
    for the CORE FOUR (pre_translating → in_translation → completed → closed, plus
    cancelled). DRAFT/IN_REVIEW ship in the enum but are manual-only for now.

    Terminal semantics (digest §item-2):
      COMPLETED — translation/workflow done, still EDITABLE + reopenable (a new
                  locale/content flips it back to IN_TRANSLATION — living projects);
      CLOSED    — terminal read-only-ish target of the 5-day auto-close clock, still
                  reopenable by adding content/a locale;
      CANCELLED — terminal, manual.
    Default for a NEW project is IN_TRANSLATION (mirrors the pre-P1 implicit state).
    """

    DRAFT = "draft"
    PRE_TRANSLATING = "pre_translating"
    IN_TRANSLATION = "in_translation"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    ALL = [  # noqa: RUF012
        "draft",
        "pre_translating",
        "in_translation",
        "in_review",
        "completed",
        "closed",
        "cancelled",
    ]
    # Statuses hidden from the default dashboard filter (the maintainer's spec: default =
    # "everything BUT done or closed"). Also covers cancelled (terminal).
    HIDDEN_BY_DEFAULT = ["completed", "closed", "cancelled"]  # noqa: RUF012
    LABELS = {  # noqa: RUF012
        "draft": "Draft",
        "pre_translating": "Pre-translating",
        "in_translation": "In translation",
        "in_review": "In review",
        "completed": "Completed",
        "closed": "Closed",
        "cancelled": "Cancelled",
    }


class Scope:
    GLOBAL = "global"
    CUSTOMER = "customer"
    PROJECT = "project"


def _utcnow() -> datetime:
    return datetime.utcnow()


# --- core hierarchy ---
class Customer(SQLModel, table=True):
    """A first-class customer entity (P4, item 5). Beyond a name, a customer carries
    saved preferences, the language pairs it uses, and scoped defaults (provider,
    workflow, segmentation profile) that pre-fill project creation and gate the
    customer's own scoped connections/workflows.

    `settings` is a free-form JSON preferences blob (notes, primary/technical
    contacts, per-customer knobs) surfaced + edited on the 360 page. `active_pairs`
    is an explicit list of "src->tgt" language-pair strings the customer works in
    (the XTM per-customer language-combinations convention) — a soft preference, NOT
    a hard gate: the 360 page shows it alongside the pairs DERIVED automatically from
    the customer's projects. `default_provider` is an EngineProfile name/id (or a
    built-in provider name) inherited at project creation; `default_workflow_id` +
    `default_segmentation_profile_id` (the latter a forward ref to P6, nullable — the
    column ships now so P6 needs no migration) are the scoped defaults.

    ISOLATION (item 5): a customer's data is isolated from other customers'. TM is
    already per-project (= per-customer transitively); connector secrets are already
    per-connection (Fernet). This layer adds Connection.customer_id + reuses the
    existing Workflow.customer_id so a customer's scoped connections/workflows are
    only ever offered inside THAT customer's project contexts. `org_id` is the
    OUTER multi-org tenant boundary (gate off by default); customer scoping layers
    UNDER it — the 360 queries stay org-scoped via access.org_filter."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # --- customer 360 (P4, item 5) ---
    settings: dict | None = Field(default=None, sa_column=Column(JSON))
    active_pairs: list | None = Field(default=None, sa_column=Column(JSON))
    default_provider: str | None = Field(default=None)
    default_workflow_id: int | None = Field(default=None, foreign_key="workflow.id")
    # Forward ref to P6 (SegmentationProfile) — nullable column ships now so the P6
    # build needs no customer migration. No FK constraint yet (the table doesn't
    # exist until P6); it's a plain nullable int until then.
    default_segmentation_profile_id: int | None = Field(default=None)
    # Content types (per-customer domain scoping). NULL = no default; a non-NULL id
    # pre-fills the new-project dialog's content-type select (inherited at project
    # creation, same inherit-at-create pattern as default_provider above). The
    # contenttype table exists in the SAME migration that adds this column, so this
    # is a REAL FK (unlike default_segmentation_profile_id, whose target table did
    # not exist yet when its column shipped).
    default_content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    created_at: datetime = Field(default_factory=_utcnow)
    # Archive/retire (SPEC-customer-archive): NULL = ACTIVE (the default, every
    # existing customer). A non-NULL timestamp marks the customer ARCHIVED — it
    # disappears from every "available customer" PICKER (project creation,
    # connection/glossary/content-type/workflow scoping, user binding, ...) and from
    # the default customers-list view, but keeps existing projects/connections/bound
    # users working untouched (archive is a selection/listing concern, NOT a kill
    # switch). Customer DELETE stays separate + fail-closed; archive is how you retire
    # a customer that has audit history. Existing rows backfill NULL = byte-identical.
    archived_at: datetime | None = Field(default=None)
    # AI opt-out (HECVAT DPAI-08): True = no content of ANY project of this customer is
    # sent to an AI engine or model (app/services/ai_optout.py). A project cannot undo a
    # customer opt-out. Existing rows backfill False = AI allowed = byte-identical.
    ai_opt_out: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    # MT formality default per TARGET LANGUAGE (#617 item 7, ADR 0062): {"de": "sie", ...}.
    # NULL = no default (no instruction, byte-identical). Resolved after document and project
    # entries by app/services/formality.py.
    formality: dict | None = Field(default=None, sa_column=Column(JSON))


class ContentType(SQLModel, table=True):
    """A small, first-class, org/customer-scoped taxonomy object (e.g. "Marketing",
    "Product UI", "Legal", "Support") — per-customer DOMAIN scoping for glossaries +
    TM (SPEC-content-types).

    Follows the repo's established nullable-scoping idiom (nullable `customer_id` +
    nullable `org_id`, NULL = global / offered-everywhere — same as Glossary,
    WorkflowGroup, PromptTemplate, ProjectTemplate). A project binds to exactly one
    content type at creation (Project.content_type_id), and that assignment drives
    the domain scoping of the project's language assets.

    THE BACKWARD-COMPAT INVARIANT (repo law for this build): with zero ContentType
    rows defined, every code path behaves byte-identically to today — all new columns
    are NULL and every new predicate is `(x IS NULL OR x = ?)`-shaped with NULL input.

    Uniqueness: (org_id, customer_id, name) — case-insensitively, a customer cannot
    have two content types with the same name, and two GLOBAL types cannot share a
    name within an org. A plain UNIQUE(org_id, customer_id, name) would be INERT in
    the shipping single-tenant config (org_id is always NULL with the multi-org gate
    off, and SQL treats NULLs as DISTINCT, so it would never fire). Instead the DB
    enforces a FUNCTIONAL unique index over
    (COALESCE(org_id,-1), COALESCE(customer_id,-1), lower(name)) — NULL-collapsed +
    case-folded — so the database is the real authority even single-tenant, on BOTH
    SQLite (expression indexes since 3.9) and Postgres. content_types.create()/rename()
    still pre-check (a friendly error instead of a raw IntegrityError) with the SAME
    case-fold, but the index is the backstop against a concurrent double-insert.
    """

    __table_args__ = (
        Index(
            "ux_contenttype_scope_name",
            text("coalesce(org_id, -1)"),
            text("coalesce(customer_id, -1)"),
            text("lower(name)"),
            unique=True,
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    created_by: str = ""  # email of the creator
    created_at: datetime = Field(default_factory=_utcnow)


class Project(SQLModel, table=True):
    # Department/actor inbox re-key (SPEC-rename-delete): the ONE name-based lookup
    # in the app (routing.ensure_department_inbox) matched inbox projects by
    # `Project.name ==`, so renaming an inbox forked a duplicate on the next inbound.
    # `inbox_key` is a stable key ("dept:<lower>" for a department inbox, "actor:<a>"
    # for a per-actor inbox) the lookup keys on instead, so name becomes display-only.
    # NULL for every ordinary project (the vast majority); a FUNCTIONAL unique index
    # over (coalesce(org_id,-1), inbox_key) makes at most one inbox per key per org
    # (NULLs stay DISTINCT so ordinary NULL-key projects never collide), on BOTH
    # SQLite (expression indexes) and Postgres — mirroring ux_contenttype_scope_name.
    __table_args__ = (
        Index("ux_project_inbox_key", text("coalesce(org_id, -1)"), text("inbox_key"), unique=True),
    )
    id: int | None = Field(default=None, primary_key=True)
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    name: str
    inbox_key: str | None = Field(default=None)
    # --- project type (SPEC-key-based-projects section 2.1) ---
    # "document" (the default, and what every pre-Batch-4 row backfills to) or
    # "key_based". The content_type_id idiom: a new dimension whose default value
    # leaves every existing code path, page, API response and export unchanged.
    # NOT NULL with a server default rather than nullable-means-document, because
    # "which kind of project is this" is a real value, never an absence, and the
    # read sites are predicates (is_key_project) that must never see NULL.
    # IMMUTABLE after creation in v1 — content.project_create sets it, nothing updates it.
    #
    # The SERVER default is carried here as well as in the migration and the legacy
    # ADD-COLUMN shim (review A13/B15): without it, create_all() built the column with
    # a Python-side default only, so the three build paths disagreed and a raw
    # `INSERT INTO project (...)` omitting project_type — exactly what a migration
    # test, a fixture or a psql session does — failed NOT NULL on a create_all DB while
    # succeeding on a real one.
    project_type: str = Field(
        default=ProjectType.DOCUMENT, sa_column_kwargs={"server_default": ProjectType.DOCUMENT}
    )
    # --- intake (SPEC-connected-projects section 4) ---
    # "upload" (default, every existing row) or "connected". NOT NULL with a server default
    # for the same reason as project_type above; may change only while the project is empty
    # (services/connected_projects.set_intake).
    intake: str = Field(
        default=ProjectIntake.UPLOAD, sa_column_kwargs={"server_default": ProjectIntake.UPLOAD}
    )
    source_locale: str = "en-US"
    default_provider: str = "claude"
    default_workflow_id: int | None = Field(default=None)
    # --- per-language-pair provider selection (Phase 2, Intento-aligned) ---
    # UNIFORM (default) = every target pair uses default_provider — byte-identical
    # to the pre-Phase-2 behavior. PER_PAIR = each (source, target) pair can name
    # its OWN provider via ProjectPairProvider rows; a pair with no row falls back
    # to default_provider, then the global default (see services/pair_providers).
    # Stored as a plain string enum for SQLite friendliness (ProviderMode).
    provider_mode: str = "uniform"
    # --- segmentation profiles (P6, item 8) ---
    # NULL = inherit the resolution chain (customer default → system default →
    # built-in). A non-NULL id is the per-project override (the maintainer's "untick default
    # segmentation" ask) — segmentation_profiles.resolve short-circuits to it.
    segmentation_profile_id: int | None = Field(default=None, foreign_key="segmentationprofile.id")
    # --- content type (per-customer domain scoping, SPEC-content-types) ---
    # NULL = the shared "General" bucket (byte-identical to today — every existing
    # project is NULL). A non-NULL id binds the project to one ContentType, chosen at
    # creation (web/CLI/api) and editable in project settings; it drives the domain
    # scoping of the project's glossaries + TM (phases 2-3). REAL FK: the contenttype
    # table exists in the same migration that adds this column.
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    created_by: str = ""  # email of the user who created the project
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # --- lifecycle (P1, items 2+3) ---
    status: str = ProjectStatus.IN_TRANSLATION
    status_changed_at: datetime | None = Field(default=None)  # last transition
    completed_at: datetime | None = Field(default=None)  # first COMPLETED — the auto-close anchor
    # --- optional due date (Feature A) ---
    # NULL = no due date = the default (most projects are fully automated and need
    # none). Stored as a naive-UTC datetime at the day boundary (a YYYY-MM-DD input
    # is stored as that day's midnight UTC) so existing datetime comparisons work
    # and there is never a naive-vs-aware mismatch (the _utcnow convention).
    due_date: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    # Archive/retire (residual sweep: archive verbs for test residue; the
    # Customer.archived_at precedent). NULL = ACTIVE (the default, every existing
    # project backfills to NULL, byte-identical). A non-NULL timestamp marks the project
    # ARCHIVED: it drops out of the default project listings + pickers and is BLOCKED
    # from launching new runs/dispatches, while its documents / segments / TM / run
    # history are fully PRESERVED and it is reversible (unarchive clears the stamp).
    # Archive is a selection/listing + launch-gate concern, NOT a delete and NOT a
    # kill switch for work already in flight (which finishes untouched). Additive +
    # purely nullable — no backfill, no index (read as `archived_at IS NULL`).
    archived_at: datetime | None = Field(default=None)
    # AI opt-out (HECVAT DPAI-08): True = no content of this project is sent to any AI
    # engine or model, local or remote (MT, LLM steps, QE/LQA judges, auto-fix, Assistant,
    # embeddings, LLMemory, OCR/vision). Enforced at the provider call boundary by
    # app/services/ai_optout.py. The customer flag applies on top (effective = either).
    ai_opt_out: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    # MT formality per TARGET LOCALE of this project (#617 item 7, ADR 0062): {"de-DE": "du"}.
    # NULL = inherit the customer default. Overrides the customer, is overridden by a document.
    formality: dict | None = Field(default=None, sa_column=Column(JSON))


class TargetLocale(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    locale: str
    # Connected projects: "stop this language". A language of a project that has a
    # ProjectAutomation binding is never deleted when it is removed: the row stays (its
    # segments, TM and exports with it) and this timestamp marks it as no longer active,
    # so runs stop pulling, translating and writing it. NULL = active (every plain project).
    stopped_at: datetime | None = Field(default=None)


class ProjectPairProvider(SQLModel, table=True):
    """A per-language-pair provider override for a project (Phase 2).

    One row per (project_id, source_locale, target_locale) naming the provider
    that pair should translate with — a built-in name (claude/gemini/openai/...)
    or an "engine:<id>" EngineProfile. Only consulted when the owning project's
    provider_mode is PER_PAIR; ignored under UNIFORM. The (source, target) key
    mirrors the WorkflowGroup per-pair-variant precedent so a later benchmark
    router (resolve_engine) can plug into the same shape. Locales are stored
    canonicalized (es_ES -> es-ES) and compared via languages_catalog.normalize,
    so a legacy underscore spelling never misses its row.
    """

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    source_locale: str
    target_locale: str
    provider: str
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectTmSource(SQLModel, table=True):
    """An ATTACHED translation-memory source: project `project_id` ALSO matches the
    TM rows that belong to project `source_project_id` (SPEC "attach an established
    TM/glossary", 2026-08-03 feedback item 5).

    REFERENCE, NOT COPY. No SegmentTM/FullTextTM/SegmentEmbedding row is duplicated,
    re-stamped or moved. The link only WIDENS the read scope every TM lookup path
    already funnels through (tm._tm_read_scope), so deleting the link restores the
    previous behaviour byte-identically. WRITE-back, `tm.count`, TMX export and the
    /tm browse are all UNCHANGED and still own-project only: a project never starts
    writing into, counting, or exporting somebody else's memory.

    THE LINK IS A HINT, NOT AN AUTHORITY. A row here is never trusted on its own at
    read time: tm._tm_source_project_ids re-derives the allowed set from the LIVE
    Project rows through access.cross_project_scope_allows — the same predicate the
    write path enforces — so a stale link (the source moved customer/org afterwards)
    or a hostile one (planted straight into the DB) contributes ZERO rows. See
    project_sources.allowed_tm_source_ids for the full argument.

    ONE LEVEL, NEVER TRANSITIVE. Only rows whose `project_id` IS the reading project
    are consulted; a source's own sources do NOT chain. Deliberate: transitivity would
    make the reachable set unbounded and un-auditable from the project page, and each
    hop would need its own scope proof. Attach each source you want, explicitly.
    """

    __table_args__ = (
        Index("ux_projecttmsource_pair", "project_id", "source_project_id", unique=True),
    )
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    source_project_id: int = Field(foreign_key="project.id", index=True)
    created_by: str = ""  # email of whoever attached it
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectGlossary(SQLModel, table=True):
    """A PROJECT-LEVEL PIN of an established module Glossary (same feedback item).

    The glossary counterpart of ProjectTmSource, but a different mechanism because
    glossaries already have a first-class container: this pins an EXISTING Glossary to
    a project so every run of that project matches its terms, without a workflow step
    having to name it.

    Precedence (glossary_module.build_glossary_plan): an explicit STEP pin still wins
    and stays an exclusive override, byte-identical to today. Otherwise project pins
    come FIRST (an explicit choice outranks an implicit auto-attach), then the typed
    project's auto-attached glossaries, then the legacy per-project entries.

    A pin is EXPLICIT, so it fires on an UNTYPED project too — the content-type
    auto-attach gate exists to keep IMPLICIT attach from firing where nobody asked for
    it, and nobody asked here either until they clicked Attach. With no pin rows the
    plan builder is byte-identical to before this table existed.

    Approval is still load-bearing: a DRAFT pinned glossary contributes NOTHING until
    it is approved (the pin records intent, not a bypass), and org/customer visibility
    is re-derived at RUN time from the live Glossary row, not trusted from the link.
    """

    __table_args__ = (Index("ux_projectglossary_pair", "project_id", "glossary_id", unique=True),)
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    glossary_id: int = Field(foreign_key="glossary.id", index=True)
    created_by: str = ""  # email of whoever pinned it
    created_at: datetime = Field(default_factory=_utcnow)


class Document(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # Set once at upload. NULL preserves the legacy {id}.{fmt} storage identity.
    upload_blob_key: str | None = None
    # `index=True` (SPEC-wave3-collaboration §8.5): the reviewer worklist's quality
    # section joins on it, and it was a bare FK while its siblings carry an index.
    project_id: int = Field(foreign_key="project.id", index=True)
    filename: str
    fmt: str = "docx"
    mode: str = TransMode.SEGMENTED
    status: str = DocStatus.NEW
    created_by: str = ""  # email of the user who uploaded the document
    # The structural "skeleton" with placeholders for translatable content.
    skeleton: dict | None = Field(default=None, sa_column=Column(JSON))
    # Request/doc-level context package (SPEC-context-metadata phase 1): the
    # validated+sanitized TranslationContext (context_pkg.validate_and_sanitize) that
    # arrived with this document (API `context` field, connector override, or the web
    # upload form). NULL = no context (byte-identical to today — every existing doc).
    # Written at INGESTION; USED at prompt time (resolve() layers it over profiles).
    context: dict | None = Field(default=None, sa_column=Column(JSON))
    # MT formality override for this document per TARGET LOCALE (#617 item 7, ADR 0062).
    # NULL = inherit the project setting. The most specific level of the resolution order.
    formality: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class Segment(SQLModel, table=True):
    # Key-based projects (SPEC-key-based-projects section 2.4): a key's VALUE is a
    # Segment row like any other, so the orchestrator, TM, glossary, LLMemory, LQA,
    # the CAT editor, the export tag guard and every isolation predicate apply to key
    # content with ZERO pipeline changes. The identity of such a row is
    # (key_id, target_locale, plural_category) instead of (document, locale, order),
    # and that is enforced by a PARTIAL unique index so document rows (key_id NULL)
    # are not indexed at all -- they keep ux_segment_doc_locale_order as their only
    # uniqueness rule, byte-identically to before. coalesce() on plural_category
    # because NULLs are DISTINCT in SQL, which would let two non-plural rows for the
    # same (key, locale) coexist. Partial + expression indexes work on both SQLite
    # and Postgres (the two engines CI runs).
    __table_args__ = (
        Index(
            "ux_segment_key_locale_form",
            text("key_id"),
            text("target_locale"),
            text("coalesce(plural_category, '')"),
            unique=True,
            sqlite_where=text("key_id IS NOT NULL"),
            postgresql_where=text("key_id IS NOT NULL"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    target_locale: str
    order_index: int = 0
    source_text: str = ""
    target_text: str = ""
    status: str = SegStatus.NEW
    mt_confidence: float | None = None
    tm_match_pct: int | None = None
    inline_tags: list | None = Field(default=None, sa_column=Column(JSON))
    qa_flags: list | None = Field(default=None, sa_column=Column(JSON))
    tte_ms: int | None = None  # Time-to-Edit, captured in the CAT editor
    # Per-unit metadata (SPEC-context-metadata phase 1): column ships NOW, actively
    # WRITTEN in later phases (phase 2+ per-unit overrides — char_limit, key/id,
    # neighbour refs). NULL on every row today = byte-identical; nothing reads it this
    # phase (created so the phase-2 build needs no migration). NOTE (Wave 3): reviewer /
    # vendor CONVERSATION about a unit lives in the `querythread` table (SPEC-wave3-
    # collaboration §3), NOT here — this slot is for structured per-unit overrides.
    meta: dict | None = Field(default=None, sa_column=Column(JSON))
    # --- key-based projects (SPEC-key-based-projects section 2.4) ---
    # NULL on every document segment (= every row that exists today) -> byte-
    # identical. Non-NULL binds this row to a StringKey; plural_category is the CLDR
    # form name (zero|one|two|few|many|other) for a plural key, NULL otherwise.
    key_id: int | None = Field(default=None, foreign_key="stringkey.id", index=True)
    plural_category: str | None = Field(default=None)
    # --- machine-overwrite protection: two independent guards at the write seam ---
    # `quality.commit_mt_output` (the SOLE writer of machine / imported / vendor text)
    # refuses to overwrite a segment for EITHER of two reasons, so a re-run, a vendor
    # return or a key import can never silently destroy protected content:
    #   1. STATUS protection (`status` == EDITED or APPROVED) -- a HUMAN decision. Core,
    #      default-on, no kill switch, NOT gated by TM_LOCKS_ENABLED. The refusal is
    #      byte-untouching (status/target/qa_flags/lock columns all unchanged) and returns
    #      CommitOutcome.status_protected. NEEDS_REVIEW is NOT protected (machine routing --
    #      a re-run may re-translate it). Escape hatch: clear the target in the CAT editor
    #      (resets status to NEW) or edit it (stays EDITED, protects the new text).
    #   2. LOCK protection (the `lock_band`/`lock_hard` columns below) -- a TM sign-off.
    # The two are orthogonal: a hard TM lock protects a segment whose status stays MT,
    # while status protection needs no lock at all. Both leave the row exactly as stored.
    #
    # --- TM-matching locks (PROPOSAL-tm-matching-workflow-step §3/§5/§8) ---
    # A `tm_match` workflow step FILLS a qualifying TM match and LOCKS the segment so
    # later steps (MT, LQA, fix rounds) never overwrite it. Two columns carry the whole
    # lock state; provenance (supplying TM row id, entry id, matched-at) rides
    # `meta["tm_lock"]` (the per-unit metadata JSON that already ships).
    #   lock_band  — NULL (every existing row -> byte-identical, unlocked) | "ice"
    #                (101 / in-context exact) | "100" (byte-exact, context absent). The
    #                value-origin distinction §8 wants, without rippling a new SegStatus
    #                through every export/grid filter (the status stays MT).
    #   lock_hard  — True = HARD lock: an LLM/MT/import write to this segment is REFUSED
    #                at the sole write seam (quality.commit_mt_output). False = SOFT
    #                lock: excluded from downstream translate steps but still enters LQA
    #                and is editable in an LQA-flagged round-2 fix. Only a human
    #                (reviewer+) may ever overwrite a hard lock (audited unlock).
    # A NULL lock_band means unlocked regardless of lock_hard (the default False).
    lock_band: str | None = Field(default=None)
    # server_default="0" so a raw INSERT that omits lock_hard (e.g. a test/self-heal
    # path that writes segment rows via raw SQL) gets 0, NOT a NOT NULL violation --
    # the create_all path must match the migration + db.py shim, which both default it.
    lock_hard: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})


# --- key-based projects: the identity layer over the Segment spine ---
class StringKey(SQLModel, table=True):
    """One translatable STRING IDENTITY in a key-based project.

    The key is metadata + identity; its VALUES are Segment rows (see Segment.key_id)
    under the internal per-namespace Document. Identity is the tuple
    (project, branch, namespace, name, context) -- the one decision that cannot be
    refactored later (research 01 section 9.6), enforced by a PARTIAL unique index so a
    soft-deleted key never blocks re-creating its name. `context` is deliberately
    MUTABLE (Lokalise's immutable context is a documented user trap).
    """

    __table_args__ = (
        Index(
            "ux_stringkey_identity",
            text("project_id"),
            text("coalesce(branch_id, 0)"),
            text("namespace"),
            text("name"),
            text("context"),
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    # Reserved for branching (explicitly deferred, spec section 9): ALWAYS NULL in v1.
    # It is in the unique tuple NOW so adding branches later needs no identity change.
    branch_id: int | None = Field(default=None)
    # A first-class column, NOT a name prefix: "common" + "error.generic" stays two
    # queryable fields, so an import of common.json and admin.json defining the same
    # name produces two keys instead of Lokalise's silent merge.
    namespace: str = ""
    name: str  # flattened key path ("::" separator for nested JSON)
    context: str = ""  # disambiguator; MUTABLE
    description: str = ""  # translator-facing note
    char_limit: int | None = None
    is_plural: bool = False
    plural_arg_name: str | None = None
    tags: list | None = Field(default=None, sa_column=Column(JSON))
    source_file: str = ""  # provenance (import filename), informational
    is_hidden: bool = False
    is_archived: bool = False
    custom: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)  # METADATA mtime
    # MAX over child value changes -- Lokalise's deliberate split between metadata
    # churn (tags/description) and content churn. NULL = no value has ever changed.
    translations_modified_at: datetime | None = Field(default=None)
    # SOFT DELETE: required by the delta feed. A hard delete makes tombstones (and
    # therefore cleanup-mode imports) structurally impossible.
    deleted_at: datetime | None = Field(default=None)


class KeyChangeLog(SQLModel, table=True):
    """The Delta API's backing store -- an append-only change feed with FIRST-CLASS
    TOMBSTONES (the thing no surveyed competitor ships; research 01 section 6).

    The PRIMARY KEY IS THE CURSOR: a global autoincrement filtered per project
    (`WHERE project_id = ? AND id > ? ORDER BY id`). Not a timestamp (ties + drift)
    and not a per-project counter (racy). Every mutation of a key or a value appends
    exactly one row, including deletions, so a `since=` consumer learns about a
    removal by RECEIVING a row rather than by noticing an absence.

    `key_id` carries NO foreign key on purpose: the row must outlive the key it
    describes (a tombstone whose FK cascaded away would delete the very evidence the
    feed exists to deliver). namespace/name are denormalized for the same reason --
    a tombstone has to be self-describing after the key row is gone.

    This is NOT the AuditLog and does not join its HMAC chain (frozen input set).
    """

    __table_args__ = (Index("ix_keychangelog_project_cursor", "project_id", "id"),)
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    entity: str = KeyEntity.KEY  # "key" | "value"
    key_id: int  # survives key soft-delete (no FK cascade)
    namespace: str = ""
    name: str = ""
    target_locale: str | None = None  # NULL for entity="key" rows
    plural_category: str = ""  # "" for non-plural
    op: str = KeyOp.CREATED  # created | updated | deleted
    changed_at: datetime = Field(default_factory=_utcnow)
    # WHO made this change. Four grammars, and they are a CONTRACT a delta consumer
    # reads (SPEC-key-based-projects section 5):
    #   ""                                    an unattributed write (a fixture, or a
    #                                         writer running on a thread POOL -- see
    #                                         keys.actor_scope's stated limit)
    #   "<email>" / "<api-key label>"         a human or a token
    #   "cli"                                 the in-process CLI
    #   "workflow:wf<id>:<locale> by <user>"  a workflow WRITE-BACK. Not
    #                                         `workflow:<run_id>`: the run id does not
    #                                         exist until start_run has created the row
    #                                         AND started the thread that snapshots the
    #                                         actor, and closing that gap means editing
    #                                         orchestrator.py. The `workflow:` PREFIX is
    #                                         the load-bearing part (machine write vs
    #                                         human edit); the rest names the launch and
    #                                         the human who asked for it.
    actor: str = ""


class KeyImage(SQLModel, table=True):
    """A raw in-context image attached to ONE string key (Batch 14, per-string image
    translation context). The pixels ride the translation call to a vision-capable
    engine as CONTEXT beside the source text -- never OCR'd, never turned into a caption
    or a prompt string (LAW-IMG-5). Storage is a Fernet-at-rest BlobStore blob keyed by a
    server-derived flat uuid; the row carries only metadata + the blob key.

    IDENTITY (LAW-IMG-1): an image attaches by ``key_id`` -> ``stringkey.id`` only; no
    image column enters ``ux_stringkey_identity``. ``project_id`` is a DENORMALIZED tenant
    anchor (LAW-IMG-2), set SERVER-SIDE from the parent key's project on insert -- never
    from caller input -- so every read can assert the full ``image.key_id == key_id AND
    key.project_id == project.id AND image.project_id == key.project_id`` triple.

    ORPHAN-FREE ORDERING (LAW-IMG-16): the row is committed with ``ready=False`` BEFORE the
    blob is put, then flipped ``ready=True`` in a second commit. A crash between the two
    leaves a findable, skippable stub -- never a blob no committed row references. Send /
    list consider only ``ready=True`` rows. The stored blob is ALWAYS the upload-time
    downscaled + re-encoded PNG (<= CONTEXT_MAX_EDGE), so send-time is a pure blob read.
    """

    # DB-level backstops for the atomic cap + dedup (LAW-IMG-11, H-1). Partial-unique on
    # the LIVE rows only (WHERE deleted_at IS NULL, the ux_stringkey_identity idiom), so a
    # soft-deleted row never blocks a re-attach:
    #   ux_keyimage_key_hash    -> within-key content dedup is race-proof (a concurrent
    #                              duplicate insert raises IntegrityError, not a 2nd row).
    #   ux_keyimage_key_ordinal -> the per-key cap is race-proof: ordinals live only in
    #                              0..MAX_IMAGES_PER_KEY-1, so at most MAX rows coexist per
    #                              key; a racing insert that would exceed it collides.
    __table_args__ = (
        Index(
            "ux_keyimage_key_hash",
            "key_id",
            "content_hash",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ux_keyimage_key_ordinal",
            "key_id",
            "ordinal",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    key_id: int = Field(foreign_key="stringkey.id", index=True)
    # Denormalized tenant anchor (LAW-IMG-2): the parent key's project, server-set.
    project_id: int = Field(foreign_key="project.id", index=True)
    blob_key: str = ""  # "keyimage_<uuid4hex>.png" -- server-derived (LAW-IMG-13)
    content_hash: str = Field(default="", index=True)  # sha256 of the RAW upload (dedup)
    mime: str = "image/png"  # the STORED rendition is always re-encoded PNG (section 4.4)
    fmt: str = "png"
    orig_fmt: str = ""  # the uploaded format ("png"|"jpg"|"jpeg"), informational
    byte_size: int = 0  # bytes of the STORED (downscaled) blob
    width: int = 0
    height: int = 0
    orig_width: int = 0
    orig_height: int = 0
    caption: str = ""  # translator-facing note (scrubbed on any future export)
    ordinal: int = 0  # 0..MAX_IMAGES_PER_KEY-1
    is_primary: bool = False  # sent by default (S3)
    # Per-key: raise the per-request cap from primary-only to ALL images (S3). Default off.
    send_all_images: bool = False
    ready: bool = False  # True only after the blob is put (LAW-IMG-16)
    uploaded_by: str = ""  # email -> GDPR sweep
    uploaded_at: datetime = Field(default_factory=_utcnow)
    deleted_at: datetime | None = Field(default=None)


# --- translation memory (two parallel stores) ---
class SegmentTM(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, foreign_key="project.id")
    source_locale: str
    target_locale: str
    source_text: str
    target_text: str
    source_hash: str = Field(index=True)
    # Content type (SPEC-content-types phase 3): STAMPED from the owning project's
    # content_type_id at write time (never retro-restamped — honest provenance). NULL =
    # the shared "General" bucket (every pre-phase-3 row backfills NULL, byte-identical
    # to today). Reads scope to own-type + NULL; see tm._tm_scope. Indexed in db.py
    # _INDEXES + the phase-3 migration.
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    created_at: datetime = Field(default_factory=_utcnow)
    # --- usage tracking (TM module) ---
    # Bumped by ONE in-place UPDATE per APPLIED TM hit (a target actually written to
    # a segment) at every apply site — see tm_module.bump_usage. Fuzzy/semantic
    # SUGGESTIONS never bump these; only real applications do.
    # use_count 0 + last_used_at NULL means a never-used entry.
    use_count: int = Field(default=0)
    last_used_at: datetime | None = Field(default=None)
    # Retention lifecycle activity, independent of translation usage.
    activity_at: datetime | None = Field(default=None)
    # TM-unit metadata (SPEC-context-metadata phase 1): column ships NOW, actively
    # WRITTEN in later phases (phase 4 — provenance system/doc/content_kind, TMX-style
    # context_prev/next hashes, formality at capture time; makes retrieval smarter +
    # TMX export richer). NULL today = byte-identical; nothing reads it this phase.
    meta: dict | None = Field(default=None, sa_column=Column(JSON))


class FullTextTM(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, foreign_key="project.id")
    source_locale: str
    target_locale: str
    source_hash: str = Field(index=True)
    source_text: str
    target_text: str
    # Content type (SPEC-content-types phase 3): stamped from the project at ft_store
    # time; NULL = the shared "General" bucket (byte-identical to today). Same scope
    # rule as SegmentTM (see tm._tm_scope).
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    created_at: datetime = Field(default_factory=_utcnow)
    activity_at: datetime | None = Field(default=None)


class SegmentEmbedding(SQLModel, table=True):
    """A vector embedding of a SegmentTM's normalized source (semantic TM
    groundwork). One row per (embedded) SegmentTM row, keyed by segment_tm_id.

    Denormalized project_id / source_locale / target_locale mirror the owning
    SegmentTM so the semantic prefilter (scope + locale pair + model) never has to
    JOIN back to segmenttm. `model` records which embedder produced the vector —
    lookups filter on it so vectors from different models are never cross-compared
    (changing the embedding model requires a re-backfill). `embedding` is the raw
    float32 vector as bytes (stdlib array('f').tobytes(); no numpy dependency),
    `dim` its length. Wired to nothing yet — the embedder seam, write-back and
    semantic lookup land in a later chunk."""

    id: int | None = Field(default=None, primary_key=True)
    segment_tm_id: int = Field(foreign_key="segmenttm.id", index=True)
    project_id: int | None = Field(default=None, foreign_key="project.id")
    # Content type (SPEC-content-types phase 3): DENORMALIZED from the parent SegmentTM
    # row (carried at upsert_embedding time, like project_id already is) so the semantic
    # prefilter scopes by content type in SQL without a JOIN back to segmenttm. NULL =
    # General bucket (byte-identical to today).
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    source_locale: str = ""
    target_locale: str = ""
    model: str = ""
    dim: int = 0
    # NOT NULL to match alembic (the baseline builds this column NOT NULL) and the
    # app's actual behaviour: `bytes` (non-Optional) with a b"" default means a row is
    # NEVER constructed with embedding=None (upsert_embedding always passes real bytes;
    # an omitted value falls back to b""). Pinning nullable=False makes
    # create_all == alembic == model with ZERO drift (the parity guard enforces this).
    embedding: bytes = Field(default=b"", sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = Field(default_factory=_utcnow)


# --- glossary / termbase ---
class GlossaryStatus:
    """Approval lifecycle for a first-class Glossary (the dedicated module).

    DRAFT    — the default on create: editable, NOT offered in the workflow-step
               dropdown. Every legacy per-project GlossaryEntry set is conceptually
               DRAFT (it has no Glossary container, so it never surfaces in the
               approved-only picker — byte-identical to pre-module behaviour).
    APPROVED — a manager/admin has blessed it. Only APPROVED glossaries whose
               language pairs cover a step's pair appear in the workflow dropdown.
               Editing an approved glossary's terms REVERTS it to DRAFT (an approved
               artifact that changes must be re-blessed — the safest state semantics,
               mirroring how a changed artifact loses its prior sign-off).
    """

    DRAFT = "draft"
    APPROVED = "approved"
    ALL = (DRAFT, APPROVED)


class Glossary(SQLModel, table=True):
    """A first-class, named glossary/termbase (the dedicated module, Phase 5-B).

    A CONTAINER that owns a set of GlossaryEntry rows (via GlossaryEntry.glossary_id)
    and a set of GlossaryPair rows (which src->tgt pairs it applies to). It is
    org-owned and optionally customer-scoped, exactly like PromptTemplate/EngineProfile,
    so a module-level list honours the same org + R2-3 customer isolation as the rest
    of the app.

    Approval (GlossaryStatus): DRAFT on create; an approver (can_manage) sets APPROVED
    (recording approved_by/approved_at). Any term edit reverts it to DRAFT so a
    published glossary that changes must be re-approved before it re-appears in the
    workflow dropdown.

    Legacy per-project GlossaryEntry rows (glossary_id NULL) are NOT owned by any
    Glossary and never surface in the module list / approved picker — the pre-module
    project glossary UI keeps working byte-identically.
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str
    status: str = GlossaryStatus.DRAFT
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    # --- content type (per-customer domain scoping, SPEC-content-types phase 2) ---
    # NULL = the shared "General" bucket — applies to EVERY project (byte-identical to
    # today; every existing glossary is NULL). A non-NULL id scopes the glossary to one
    # ContentType: it is only offered/auto-attached to projects of that content type
    # (a typed project reads {its type, NULL}; an untyped project reads {NULL} only).
    # Setting it does NOT revert APPROVED->DRAFT (scoping metadata, not content — same
    # rationale as rename). REAL FK: the contenttype table exists (phase-1 migration).
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    approved_by: str = ""  # email of the approver ("" while draft)
    approved_at: datetime | None = None
    created_by: str = ""  # email of the creator
    created_at: datetime = Field(default_factory=_utcnow)


class GlossaryPair(SQLModel, table=True):
    """A source->target language pair a Glossary applies to (Phase 5-B).

    Mirrors the ProjectPairProvider storage pattern: one row per (glossary_id,
    source_locale, target_locale), locales stored CANONICAL (es_ES -> es-ES) via
    languages_catalog and compared normalized, so a legacy underscore spelling never
    misses. The workflow-step dropdown offers a glossary for a step's pair only when
    a matching GlossaryPair exists (and the glossary is APPROVED + visible)."""

    id: int | None = Field(default=None, primary_key=True)
    glossary_id: int = Field(foreign_key="glossary.id", index=True)
    source_locale: str
    target_locale: str
    created_at: datetime = Field(default_factory=_utcnow)


class GlossaryEntry(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # NULLABLE (Phase 5-B FK fix): a LEGACY per-project entry stores a real
    # project_id (FK to project.id); a MODULE-owned entry (glossary_id set) stores
    # NULL — it is not project-bound, and a 0 sentinel violated the project.id FK on
    # Postgres. glossary_id IS NULL still discriminates legacy rows, and every legacy
    # filter is `glossary_id IS NULL AND project_id == pid`, so a NULL-project module
    # row can never leak into a project's legacy view.
    project_id: int | None = Field(default=None, foreign_key="project.id")
    # Module ownership (Phase 5-B): NULL = a LEGACY per-project entry (today's rows,
    # byte-identical — matched by project_id at run time, never in the module list).
    # A non-NULL glossary_id makes the entry OWNED by a first-class Glossary; the
    # module edits/lists these and the workflow dropdown feeds match_terms(glossary_id=).
    glossary_id: int | None = Field(default=None, foreign_key="glossary.id", index=True)
    source_locale: str
    target_locale: str
    source_term: str
    target_term: str = ""  # base/default translation; senses override in context
    dnt: bool = False  # Do Not Translate
    case_sensitive: bool = False
    # Term metadata (SPEC-context-metadata phase 1): column ships NOW, actively
    # WRITTEN in later phases (phase 4 — TBX-anchored part_of_speech, definition,
    # usage_example, subject_field; senses/DNT already exist and stay). NULL today =
    # byte-identical; nothing reads it this phase.
    meta: dict | None = Field(default=None, sa_column=Column(JSON))
    # Concept model (SPEC-terminology-backend T1, LAW-TERM-2/2a): the derived-compat
    # link into the concept/term store. concept_id = the base GlossaryConcept this
    # compat row projects; term_id = the SOURCE-side GlossaryTerm it projects. BOTH
    # NULL on a legacy per-project entry (glossary_id NULL, LAW-TERM-2a) and on any
    # not-yet-backfilled row — the columns are inert until linked, so a read seam
    # (match_terms/gates/grid/CSV/api) never sees them (LAW-TERM-1). The SQLite shim
    # (db._ADDED_COLUMNS) adds these FK-LESS (SQLite cannot ADD COLUMN ... REFERENCES);
    # alembic + Postgres carry the real FK (M6/M19). Nullable → additive-in-effect.
    concept_id: int | None = Field(default=None, foreign_key="glossaryconcept.id")
    term_id: int | None = Field(default=None, foreign_key="glossaryterm.id")
    # Authority inversion (SPEC-terminology-backend T1b, LAW-TERM-18, §A.1/§A.3): the
    # per-pair BINDING now lives on this row and the concept store is the single authority;
    # the projector (glossary_concepts.project) is the sole writer of the DERIVED columns
    # above (source_term/target_term/meta). target_term_id = the ENFORCED target GlossaryTerm
    # this pair binds (NULL = a DNT-only / empty-target binding, target_term == ""). BOTH shim
    # columns are FK-less on the SQLite legacy shim (db._ADDED_COLUMNS); alembic + Postgres
    # carry the real FK. Nullable -> additive-in-effect; a not-yet-backfilled row reads NULL
    # and is self-healed by backfill_pair_bindings (A.5). meta_overrides = the per-pair meta
    # AUTHORITY, three-valued per key (absent = derive from the concept/source term; string =
    # override; JSON null = CLEARED tombstone), from which entry.meta is projected (A.3).
    target_term_id: int | None = Field(default=None, foreign_key="glossaryterm.id")
    meta_overrides: dict | None = Field(default=None, sa_column=Column(JSON))


class GlossarySense(SQLModel, table=True):
    """A context-dependent alternative translation for a term."""

    id: int | None = Field(default=None, primary_key=True)
    glossary_entry_id: int = Field(foreign_key="glossaryentry.id")
    sense_label: str  # e.g. "finance", "anatomy"
    context_cue: str = ""  # hint the LLM uses to pick this sense
    target_term: str
    # Concept model (SPEC-terminology-backend T1, §4.4/§4.5, H2): a sense folds into a
    # SIBLING homonym GlossaryConcept. concept_id = that sibling concept (DISTINCT from
    # the base entry's concept_id); term_id = the sibling concept's chosen target term.
    # The reconciler UPDATEs a sense keyed by this concept_id (LAW-TERM-2/3/7) rather
    # than blindly appending, and the renderer's selector resolves a sense-selected
    # match to the sibling's metadata/audience. BOTH NULL on a legacy/unlinked sense
    # (falls back to the base entry's concept, LAW-TERM-2a). FK-less on the SQLite shim.
    concept_id: int | None = Field(default=None, foreign_key="glossaryconcept.id")
    term_id: int | None = Field(default=None, foreign_key="glossaryterm.id")


class TermStatus:
    """Status of a GlossaryTerm within its concept (SPEC-terminology-backend §4.3).

    preferred / admitted are USABLE targets; deprecated / forbidden are do-not-use
    (rendered as negative hints by the T3 renderer, never materialized as a compat
    target — §4.4). Validated in the service, not a DB CHECK (SQLite parity)."""

    PREFERRED = "preferred"
    ADMITTED = "admitted"
    DEPRECATED = "deprecated"
    FORBIDDEN = "forbidden"
    ALL = (PREFERRED, ADMITTED, DEPRECATED, FORBIDDEN)
    # The two statuses that can back a compat target (§4.4-1 ladder), preference order.
    USABLE = (PREFERRED, ADMITTED)


class TermType:
    """Term type vocabulary (SPEC-terminology-backend §4.3): the TBX-Basic termType
    picklist plus the two audience-critical house types `ui`/`label` (27sin00 — UI
    terms are excluded from MT/LLM feeds). A free custom string is also accepted and
    stored verbatim; on TBX export (T2) a non-vocabulary type falls to the private ns."""

    FULL_FORM = "fullForm"
    ACRONYM = "acronym"
    ABBREVIATION = "abbreviation"
    SHORT_FORM = "shortForm"
    VARIANT = "variant"
    PHRASE = "phrase"
    UI = "ui"
    LABEL = "label"
    ALL = (FULL_FORM, ACRONYM, ABBREVIATION, SHORT_FORM, VARIANT, PHRASE, UI, LABEL)


class TermAudience:
    """Prompt-audience channels a term may reach (SPEC-terminology-backend §4.3,
    Decision B/F). Stored as a canonical, sorted, comma-joined subset of these three;
    the migration default (and empty-string coercion) is all three."""

    HUMAN = "human"
    MT = "mt"
    LLM = "llm"
    # Canonical stored order (gate round-1 L2): human,mt,llm — matches §4.2's literal
    # default, §7.1/M27's CSV example, and the descriptors/web-form defaults.
    ALL = (HUMAN, MT, LLM)
    DEFAULT = "human,mt,llm"  # Decision F: migrated/blank audience = all three


class GlossaryConcept(SQLModel, table=True):
    """A terminology CONCEPT — the concept level of the concept/term store
    (SPEC-terminology-backend §4.1, Decision A/E). Owns GlossaryTerm rows (one per
    locale-and-surface). It is an ADDITIVE structure kept in lock-step with the derived
    GlossaryEntry/GlossarySense compat rows (LAW-TERM-1/2); no read seam consults it in
    T1/T2 (the T3 renderer is the first reader, and only when its per-step toggle is ON)."""

    id: int | None = Field(default=None, primary_key=True)
    # Container FK, NOT NULL: a concept always belongs to a module Glossary; legacy
    # per-project entries have NO concept (LAW-TERM-2a), so this never needs a
    # project_id mode.
    glossary_id: int = Field(foreign_key="glossary.id", index=True)
    external_id: str = ""  # TBX conceptEntry id (stable round-trip, LAW-TERM-3); "" = none
    domain: str = ""  # TBX subjectField (concept level)
    subdomain: str = ""  # house sub-scope (no TBX-Basic home -> private ns on export)
    definition: str = ""  # TBX <descrip type="definition"> at concept level
    notes: str = ""  # free text / ITS localization note
    cross_refs: list | None = Field(default=None, sa_column=Column(JSON))  # [external_id,...]
    source: str = ""  # provenance (import origin / extractor id)
    homonym_group: str = ""  # non-empty groups concepts that share a source term (senses, §4.5)
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime | None = None

    # Named indexes (§4.1/M5): the single-column ix_glossaryconcept_glossary_id is the
    # auto-name SQLAlchemy emits for glossary_id's Field(index=True) — declared here too
    # so create_all and alembic agree; ix_glossaryconcept_extid is a NON-UNIQUE composite
    # (a blank external_id is legal + repeatable). verify_migrations checks named indexes.
    # ix_glossaryconcept_hgroup (T1b, §A.1): the homonym-group closure query
    # (project's group-closed scope) runs `WHERE glossary_id=? AND homonym_group=?`; this
    # NON-UNIQUE composite keeps it from a table scan. Declared here (create_all path) AND in
    # the T1b revision as a plain op.create_index; NOT on the db._ADDED_COLUMNS shim path
    # (r7-11, accepted performance-only), and ADDED to verify_migrations' index-name list.
    __table_args__ = (
        Index("ix_glossaryconcept_extid", "glossary_id", "external_id"),
        Index("ix_glossaryconcept_hgroup", "glossary_id", "homonym_group"),
    )


class GlossaryTerm(SQLModel, table=True):
    """A single TERM within a concept — the term level of the concept/term store
    (SPEC-terminology-backend §4.2). Each term carries its own status/audience/type/
    part-of-speech/gender/number/notes/DNT and provenance. Stored VERBATIM for `term`
    (the authoritative matchable surface, §4.2 boundary); free-text metadata fields are
    flatten+capped at ingestion by the service. Validation lives in glossary_concepts.py,
    NOT a DB CHECK (SQLite parity)."""

    id: int | None = Field(default=None, primary_key=True)
    # NOT index=True — the explicit __table_args__ Index below owns the single named
    # index `ix_glossaryterm_concept` (§4.2); a Field(index=True) would ALSO emit an
    # auto-named `ix_glossaryterm_concept_id`, giving two indexes create_all/alembic must
    # both declare. One named index, matching the spec.
    concept_id: int = Field(foreign_key="glossaryconcept.id")
    locale: str = ""  # "" = the documented any-locale wildcard (matches match_terms _loc_ok)
    term: str
    status: str = TermStatus.PREFERRED  # preferred|admitted|deprecated|forbidden
    term_type: str = ""  # TermType vocab (§4.3); "" = unspecified
    part_of_speech: str = ""  # noun|verb|adjective|adverb|properNoun|other|"" (validated on TBX)
    grammatical_gender: str = ""  # masculine|feminine|neuter|otherGender|""
    grammatical_number: str = ""  # singular|plural|dual|mass|otherNumber|""
    audience: str = TermAudience.DEFAULT  # canonical sorted CSV subset of {human,mt,llm}
    usage_note: str = ""  # TBX <termNote type="usageNote">
    context_example: str = ""  # TBX <descrip type="context">
    geographical_usage: str = ""  # TBX <termNote type="geographicalUsage">
    case_sensitive: bool = False
    exact: bool = False  # STORED + round-tripped in JSON/TBX; NOT wired to the gate in T1 (E3/H4)
    dnt: bool = False  # source-side only (a dnt on a target term is meaningless)
    source: str = ""  # TBX <admin type="source">
    deprecated_replacement_term_id: int | None = Field(default=None, foreign_key="glossaryterm.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime | None = None

    __table_args__ = (Index("ix_glossaryterm_concept", "concept_id"),)


class TermCandidateStatus:
    """Review lifecycle for an extracted term candidate (term-extraction feature).

    PENDING  — the default on extraction: awaiting a reviewer's decision. Shown in the
               candidate-review grid. Postponing a candidate simply leaves it PENDING.
    ACCEPTED — a reviewer accepted it into the owning glossary as a real GlossaryEntry
               (customer-bound by construction). Kept as a durable audit trail of the
               decision; never re-proposed.
    DROPPED  — a reviewer rejected it. Kept PER-GLOSSARY so re-extraction NEVER
               re-proposes a dropped term (the memoQ self-teaching-stopword lesson —
               dropping teaches the filter). Never re-proposed.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    DROPPED = "dropped"
    ALL = (PENDING, ACCEPTED, DROPPED)
    # A decision that PERSISTS as noise-control history: a source term that is already
    # ACCEPTED or DROPPED for a glossary is never re-proposed by a later extraction run.
    DECIDED = (ACCEPTED, DROPPED)


class TermCandidate(SQLModel, table=True):
    """One extracted candidate term awaiting human review (the term-extraction loop).

    The upstream half of the maintainer's term-extraction idea: an extraction run over a document
    proposes candidates (LLM extract -> LLM precision-prune -> deterministic sanity
    filters), each landing PENDING in a per-glossary review grid. A reviewer accepts a
    candidate (-> a real GlossaryEntry in THAT glossary, customer-bound by construction)
    or drops it. Accepted/dropped decisions PERSIST so re-extraction never re-proposes an
    already-decided term (dropped candidates are the per-glossary noise-control backbone —
    the memoQ self-teaching-stopword pattern).

    A candidate is DATA about a proposed term; like GlossaryEntry.meta it never reaches a
    prompt. Owned by a first-class Glossary (customer-bound via that glossary), with
    optional source-document provenance. The review grid is a SHARED component: a future
    upload-time extraction pass (research Option A) reuses the same grid + the same
    accept/drop service seams, pointing them at its own candidates.
    """

    id: int | None = Field(default=None, primary_key=True)
    glossary_id: int = Field(foreign_key="glossary.id", index=True)
    source_term: str
    # The LLM-proposed translation for the run's target locale (editable by the reviewer
    # before accept). NULL = no proposal (e.g. the offline mock, or a spend-degraded run
    # that produced candidates without translations).
    proposed_target: str | None = Field(default=None)
    source_locale: str = ""
    target_locale: str = ""
    # How often the source term occurred in the document the candidate came from (the
    # frequency lens — 0 when not computable). Sorted-on so real terms surface first.
    frequency: int = Field(default=0)
    # A short in-context snippet (the concordance lens) so a reviewer sees the term in use.
    context_snippet: str = ""
    status: str = TermCandidateStatus.PENDING
    # Provenance: the document this candidate was extracted from (NULL when unknown / a
    # pasted-text run). Deletion scrubs context and accepted examples before clearing this FK.
    source_document_id: int | None = Field(default=None, foreign_key="document.id")
    created_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = Field(default=None)  # when accepted/dropped
    decided_by: str = ""  # email of the deciding reviewer


# --- style guides (SPEC-workflows-redesign Track B, B2 — the flagship asset) ---
class StyleGuideStatus:
    """Approval lifecycle for a StyleGuide — the SAME two-state model as
    GlossaryStatus (draft <-> approved), mirrored deliberately (Track B, B2 spec:
    "mirror the Glossary lifecycle enum values exactly"). Kept as its own class (not an
    alias) so the two assets can diverge later without a silent coupling, but the string
    VALUES are identical on purpose — a shared vocabulary the surfaces already speak.

    DRAFT    — the default on create: editable, NEVER resolves into a prompt.
    APPROVED — a linguist/admin has blessed it. ONLY approved guides are eligible for
               resolution + injection (style_guides.resolve_style_guide). Editing an
               approved guide's BODY or SCOPE reverts it to DRAFT (a changed asset must
               be re-blessed — same safest-state semantics as the glossary module)."""

    DRAFT = "draft"
    APPROVED = "approved"
    ALL = (DRAFT, APPROVED)


class StyleGuide(SQLModel, table=True):
    """A first-class, approvable, customer-scoped STYLE GUIDE — long-form authored
    guidance (register, tone, voice, punctuation, brand rules) packaged into the
    translation prompt (SPEC-workflows-redesign Track B, B2).

    Mirrors the Glossary model's ownership + approval shape (org-owned, optionally
    customer-scoped, DRAFT/APPROVED lifecycle recording approved_by/approved_at) and
    adds the ContextProfile scope grain: it resolves at the customer x content_type
    "workhorse" grain (NOT a per-step pin), optionally narrowed to a language pair.
    Injected through the context chassis' idioms (a fenced, sanitized, budget-capped
    {style_guide} block — see app/services/style_guides.py).

    Scope columns (all nullable = the broader default):
      * customer_id     NULL = an ORG-GLOBAL guide (applies to every customer);
                        set = confined to that customer (R2-3 isolation, like Glossary).
      * content_type_id NULL = applies to every content type; set = only that type.
      * source_locale / target_locale
                        BOTH NULL = a UNIVERSAL (pair-agnostic) guide; both set = a
                        per-pair guide serving exactly that source->target direction.
                        (A guide with only one side set is treated as universal by the
                        resolver — a half-pair is meaningless; the CRUD seams keep them
                        paired.)

    THE ASSET-BODY CONTRAST WITH THE GLOSSARY-METADATA INVARIANT (documented choice):
    the glossary module deliberately EXCLUDES term metadata (definition/usage/notes)
    from the prompt (glossary.py invariant — metadata is for humans, not the model). A
    style guide is the OPPOSITE: its `body` IS the asset and its whole purpose is to
    reach the model. So the body is rendered into the prompt — but sanitized like the
    context chassis' `_san` (control chars flattened, angle brackets neutralized) so a
    hostile body can never forge the `</style_guide>` fence. Reaching the prompt is the
    point; forging structure is what's prevented. See style_guides.render_style_guide.

    SCOPE INDEX (documented decision — NON-unique, unlike ux_contextprofile_scope):
    a FUNCTIONAL index over (coalesce(org_id,-1), coalesce(customer_id,-1),
    coalesce(content_type_id,-1)) speeds the resolver's scope reads. It is NOT unique:
    multiple guides may share a scope (e.g. several drafts, or an approved guide plus a
    new draft revision), so a unique constraint would block the natural authoring flow.
    The resolver instead breaks ties deterministically — among the APPROVED guides at
    the most-specific matching scope, pair-specific beats universal, then
    most-recently-updated wins (updated_at desc, id desc). See resolve_style_guide."""

    __table_args__ = (
        Index(
            "ix_styleguide_scope",
            text("coalesce(org_id, -1)"),
            text("coalesce(customer_id, -1)"),
            text("coalesce(content_type_id, -1)"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    name: str
    status: str = StyleGuideStatus.DRAFT
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    # Per-pair scoping (nullable = universal). Stored canonical (es_ES -> es-ES) by the
    # service, compared normalized — mirrors GlossaryPair's locale handling.
    source_locale: str | None = Field(default=None)
    target_locale: str | None = Field(default=None)
    # The long-form authored guidance itself (the asset). TEXT, not a JSON blob.
    body: str = ""
    approved_by: str = ""  # email of the approver ("" while draft)
    approved_at: datetime | None = None
    created_by: str = ""  # email of the creator
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# --- workflows (the orchestration centerpiece) ---
class WorkflowGroup(SQLModel, table=True):
    """A multilingual workflow container (P5, item 4). Holds a family of per-
    language-pair Workflow variants (each unidirectional) so one customer's
    "translation workflow" can differ per language while living under one name.

    Validated by XTM's per-target-language workflow model (research_gapfill GAP 1):
    one project, per-target-language workflow variants selectable via a language
    filter. Here the grouping is explicit — a group owns N Workflow rows, each
    tagged with (source_locale, target_locale). The "copy to language pair(s)"
    action (workflows.copy_to_pairs) clones a source variant onto other pairs,
    optionally LLM-adapting each step's instruction (the market differentiator —
    no vendor does cross-language step-instruction rewriting).

    `customer_id` nullable = a GLOBAL group (offered everywhere) vs a CUSTOMER
    group (offered only on that customer's projects, per P4 scoping). `org_id`
    ships nullable from day one (multi-org gate OFF; the column exists so the gate
    needs no later migration). A Workflow points back via Workflow.group_id."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    description: str = ""
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectTemplate(SQLModel, table=True):
    """A reusable project-configuration snapshot (P7, item 9 — Phrase project
    templates + XTM per-customer templates precedent).

    Captures everything a project's setup form asks for — source locale, target
    locales, default provider/engine, default workflow, segmentation profile — so
    "next time" is one pick instead of re-entering it all. `customer_id` NULL = a
    GLOBAL template (offered everywhere) vs a customer template (offered only for
    that customer, per the P4 scoping rule — the owner's "savable by customer
    only, or global"). A template is a ONE-TIME snapshot: editing the source
    project later never mutates it, and applying one never links back (plan §g —
    no versioning/propagation).

    Referenced ids are stored WITHOUT FK constraints on purpose: a template may
    outlive its workflow/segmentation profile, and apply_template tolerates a
    dangling ref (skips it with a warning — plan §h) rather than failing the
    whole instantiation. `target_locales` is a JSON list of catalog codes,
    re-validated through content.project_create at apply time (the catalog may
    have changed since the snapshot)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # unique per install
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    source_locale: str = "en-US"
    target_locales: list | None = Field(default=None, sa_column=Column(JSON))
    default_provider: str = "claude"  # provider name or "engine:<id>" (P3)
    default_workflow_id: int | None = None  # no FK: may dangle (§h)
    segmentation_profile_id: int | None = None  # no FK: may dangle (§h)
    description: str = ""
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class SegmentationProfile(SQLModel, table=True):
    """A named segmentation ruleset attachable per customer/project (P6, item 8).

    Corroborated by XTM + memoQ + Phrase (research_gapfill GAP 2): named SRX
    rulesets scoped global (customer_id NULL) or to one customer, with a system
    default and a per-project "untick default" override. Resolution at ingest is
    the fallback chain project → customer → system-default → built-in generated
    rules (segmentation_profiles.resolve).

    `srx_blob` holds the SRX 2.0 ruleset TEXT. NULL = the BUILT-IN default (the
    shipped app/resources/default.srx, which reproduces the legacy splitter byte
    for byte) — a NULL-blob profile segments IDENTICALLY to today. A non-NULL blob
    is a custom uploaded/cloned ruleset (1MB-bounded + parse-validated on the way
    in, exactly like the /settings custom .srx upload).

    `customer_id` NULL = a GLOBAL profile (offered on every project) vs a customer
    profile (offered only on that customer's projects, per P4 scoping). Exactly one
    profile per install may carry `is_system_default` — the third rung of the
    resolution chain (segmentation_profiles.ensure_system_default seeds it).

    TM-SAFETY: the existing seg_engine sha1 stamp (srx.engine_identity) binds a
    document's segmentation to the EXACT ruleset bytes it was ingested under. A
    profile's ruleset resolves to those same bytes (blob, or the default's bytes
    when NULL), so the stamp is per-ruleset-sha unchanged — export reproduces the
    ingest-time split or falls back to legacy with a warning on a hash mismatch,
    guarding TM invalidation. Profiles EXTEND that stamp, they don't alter it.

    `org_id` ships nullable from day one (multi-org gate OFF; the column exists so
    the gate needs no later migration). Customer.default_segmentation_profile_id
    (added in P4) becomes a real reference here (validated on set)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # admin-chosen, unique per install
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    srx_blob: str | None = Field(default=None)  # SRX text; NULL = built-in default
    description: str = ""
    is_system_default: bool = False  # the system-default rung of the chain
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ContextProfile(SQLModel, table=True):
    """A standing "context card" — a stored TranslationContext package + a scope
    (SPEC-context-metadata phase 1). NOT a single per-customer default: The maintainer's
    correction (2026-07-12) is that a customer rarely translates only ONE kind of
    content, so cards are a first-class SCOPED entity, customer×content_type being
    the workhorse grain (marketing vs support vs product differ).

    Scope combinations (per the spec's table), most-specific first at resolution:
      | project_id set                         | one-off campaign override
      | customer_id + content_type_id          | THE workhorse (Acme x Marketing)
      | content_type_id (customer NULL)        | org-wide norms for a content kind
      | customer_id (content_type NULL)        | broad brand facts for a customer
      | (all NULL, org default)                | the org's baseline card
    `context` holds the validated package (context_pkg.validate_and_sanitize shape).
    `authority` records the tier that authored it (operator | api | connector) —
    phase 1 only ever writes "operator" (profiles are operator-authored via the web
    UI / CLI); the field ships now so phase-3 connector-authored cards need no
    migration.

    UNIQUENESS: exactly one profile per EXACT scope combo per org. Enforced by a
    FUNCTIONAL unique index over
    (coalesce(org_id,-1), coalesce(customer_id,-1), coalesce(content_type_id,-1),
     coalesce(project_id,-1)) — NULL-collapsed so it fires even single-tenant (SQL
    treats NULLs as DISTINCT, so a plain UNIQUE would be inert), on BOTH SQLite
    (expression indexes) and Postgres. context_pkg.upsert_profile pre-checks with the
    same key (a friendly error, not a raw IntegrityError); the index is the backstop.
    Follows the SegmentationProfile/ContentType org_id-stamping + nullable-scoping
    idiom exactly."""

    __table_args__ = (
        Index(
            "ux_contextprofile_scope",
            text("coalesce(org_id, -1)"),
            text("coalesce(customer_id, -1)"),
            text("coalesce(content_type_id, -1)"),
            text("coalesce(project_id, -1)"),
            unique=True,
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    project_id: int | None = Field(default=None, foreign_key="project.id")
    name: str = ""
    # The authoring tier (SPEC-context-metadata authority model). Phase 1: always
    # "operator" (profiles are operator-authored). Kept for the phase-3 connector tier.
    authority: str = "operator"
    context: dict | None = Field(default=None, sa_column=Column(JSON))
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Workflow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    scope: str = Scope.GLOBAL
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    project_id: int | None = Field(default=None, foreign_key="project.id")
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # --- multilingual workflow groups (P5, item 4) ---
    # group_id nullable = an UNGROUPED (universal/legacy) workflow — behaves exactly
    # as it did pre-P5 (dispatch never touches it). source_locale/target_locale
    # nullable = a UNIVERSAL variant (matches any pair inside its group, i.e. the
    # group default). A grouped variant tagged with a concrete pair serves exactly
    # that source->target direction (workflows are unidirectional per item 4).
    group_id: int | None = Field(default=None, foreign_key="workflowgroup.id")
    source_locale: str | None = Field(default=None)
    target_locale: str | None = Field(default=None)
    # is_default marks the variant a group falls back to when no exact-pair variant
    # matches a run's target (the "group default" rung of the dispatch chain).
    is_default: bool = False
    # --- origin stamp (SPEC-workflows-redesign Phase 3) ---
    # How this workflow was created: "manual" (create_workflow — the blank-workflow /
    # `workflow create` path) or "draft" (apply_workflow_draft — the Design-a-workflow
    # promptable path, INCLUDING every multilingual variant it fans out, which all
    # originate from the same draft). NULL = legacy/unknown (every row created before
    # this migration; the UI renders no origin chip for NULL). NOT derivable from
    # provenance (adapted_from_id is unpopulated on the production adapt=False path —
    # see SPEC "Corrections"), so this is a first-class stamp. Additive column only.
    created_via: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    # Archive/retire (residual sweep: archive verbs for test residue; the
    # Customer.archived_at / Project.archived_at precedent). NULL = ACTIVE. A non-NULL
    # timestamp marks the workflow ARCHIVED: it drops out of the default workflow
    # listings + pickers, is EXCLUDED from dispatch/variant resolution, and is BLOCKED
    # from launching new runs — while its steps + run history are PRESERVED and it is
    # reversible. Not a delete, not a kill switch for in-flight runs. Additive nullable.
    archived_at: datetime | None = Field(default=None)


class WorkflowStep(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    workflow_id: int = Field(foreign_key="workflow.id")
    order_index: int = 0
    name: str = ""  # free-text role, e.g. "Translator", "ICR" — not a fixed taxonomy
    step_type: str = StepType.LLM_AGENT
    provider: str | None = None
    model: str | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None
    prompt_template_id: int | None = Field(default=None, foreign_key="prompttemplate.id")
    prompt_text: str = ""  # inline prompt if not using a saved template
    retry_max: int = 2
    backoff_base: float = 1.0
    chunk_policy: str = "auto"  # auto | none
    redact_pii: bool = False  # scrub PII before this step's engine call, restore
    # after (per-step choice; the global pii_redaction
    # setting still forces redaction when it is on)
    # --- cross-language copies + shared-base provenance (P5 item 4; 4b) ---
    # adapted_from_id points at the SOURCE step this step was cloned from. Since
    # SPEC-workflows-redesign 4b it is stamped on EVERY copy_to_pairs clone (both the
    # plain and the adapt=True path), because it is the LIVE provenance link
    # shared-base propagation walks (see WorkflowStep.overridden). needs_review flags
    # "the LLM adapted this — a linguist should post-edit before activating" (the maintainer's
    # post-edit ask; Smartling's test-then-activate pattern) and is set ONLY on the
    # adapt=True path. adapted_from_id is NULL on a plain add-step, a whole-workflow
    # clone_workflow copy, and a LEGACY variant made before 4b (which is therefore
    # never a propagation target — conservative by construction).
    adapted_from_id: int | None = Field(default=None, foreign_key="workflowstep.id")
    needs_review: bool = False
    # --- shared-base + override-delta variant model (SPEC-workflows-redesign 4b) ---
    # overridden=True means this VARIANT step was deliberately diverged (PINNED): a
    # base edit must NEVER touch it. Default False (NULL-free, server_default '0').
    # TWO-KEY SAFETY (the sacred don't-clobber-a-pinned-locale invariant): a variant
    # step is only ever written by base-edit propagation when BOTH
    #   (1) adapted_from_id points at the edited base step (a LIVE provenance link,
    #       populated on EVERY clone since 4b), AND
    #   (2) overridden is False.
    # A LEGACY variant (cloned before 4b) has adapted_from_id NULL, so (1) fails and it
    # is NEVER reached by propagation — legacy rows are conservative by construction, no
    # fake backfill. A False default on an existing 4b-era clone is therefore SAFE: it
    # only means "not yet pinned", and (1) still gates whether it is a propagation target.
    # A direct edit of a variant step auto-sets this True ("you touched it, you own it");
    # a pin/unpin control sets it explicitly. Inert on a base/legacy (adapted_from NULL)
    # step — nothing propagates TO it, so overridden is never consulted for it.
    overridden: bool = False
    # --- human-review assignee (Feature B) ---
    # The reviewer responsible for a human_review step; the address the pause
    # notification email is sent to. "" (the default) = no assignee = no email
    # (v1: explicit-assignee-only, no team/role broadcast). Inert / ignored on
    # non-human_review steps. Existing rows backfill to "" (byte-identical).
    #
    # Wave 3 (SPEC-wave3-collaboration §8.3): Train D's reviewer worklist will READ
    # this to build its "paused steps that name you" section (Train D lands the
    # reader; Train A only ships the index). Assignment is a property of the workflow
    # DEFINITION, not of an individual run — so a match means EVERY paused run of that
    # workflow, not a per-run assignment. `index=True` (§8.5) backs that future join;
    # the column had no index before.
    assignee_email: str = Field(default="", index=True)
    # --- glossary selection (Phase 5-B, the module payoff) ---
    # NULL = today's behaviour: the run matches ALL of the document project's glossary
    # entries (project-scoped match_terms). A non-NULL glossary_id pins a specific
    # APPROVED module Glossary whose language pairs cover the run's pair — the
    # orchestrator then feeds match_terms(glossary_id=...) so only that glossary's
    # terms/senses apply. Set via the workflow-step dropdown (approved+pair-filtered).
    glossary_id: int | None = Field(default=None, foreign_key="glossary.id")
    # --- engine-layer quality upgrades (F2, ModernMT/Lara-derived) ---
    # BOTH default OFF so an existing step behaves byte-identically (the assembled
    # prompt is unchanged). Same per-step altitude as redact_pii / chunk_policy —
    # these are prompt-shaping choices the step already owns.
    #   adaptive_feedback: F2.1 — feed earlier confirmed source->target pairs of the
    #     SAME document into LATER segments' prompts (self-propagating terminology).
    #   adaptive_feedback_window: max prior pairs injected (default 5; 0 disables).
    #   doc_context_prepass: F2.2 — one cheap pre-pass call extracts a doc-level
    #     brief prepended (stable) to every segment prompt in the run.
    adaptive_feedback: bool = False
    adaptive_feedback_window: int = 5
    doc_context_prepass: bool = False
    # --- MQM-aligned QE score + routing gate (roadmap gap #1 + #6) ------------
    # The consensus HITL pattern (Phrase QPS / XTM Intelligent Score / Smartling
    # LQE): score EVERY produced segment 0-100 with an MQM-aligned LLM judge, and
    # route only the low-confidence ones to a human. ALL default OFF so an existing
    # step is byte-identical (no judge call, no prompt/event change).
    #   qe_gate: when True, score each segment this step produces and route those
    #     below qe_threshold to human review (SegStatus.NEEDS_REVIEW); >= threshold
    #     proceeds as today. Judge failures soft-degrade (segment proceeds, counted
    #     as qe_skipped) — the judge NEVER blocks production.
    #   qe_threshold: pass/route cutoff, 0-100 (default 70). score >= threshold
    #     proceeds; score < threshold routes to review (== threshold PROCEEDS).
    #   qe_provider: optional engine picker for the judge (a built-in provider name
    #     or "engine:<id>"). NULL = reuse the step's own provider (cheap-by-default).
    qe_gate: bool = False
    qe_threshold: int = 70
    qe_provider: str | None = None
    # --- QE auto-fix (Train B, feat/jev-autofix) --------------------------------
    # OPT-IN per step, live only when qe_gate is on AND the QE judge is a Jev judge
    # (qe_jev.is_judge_ref). A segment that FAILS the gate is handed to an LLM post-
    # editor (qe_jev_loop.refine) for up to qe_autofix_max_rounds rounds, re-scored by
    # the same judge each round; a version that passes replaces the output, anything
    # still failing goes to review with the scoring history (qa_flags "qe_autofix").
    #   qe_autofix: master toggle (default False = byte-identical, no loop).
    #   qe_autofix_provider: the fixer LLM (built-in name or engine:<id> of an LLM kind).
    #     NULL = the step's own provider. Refused at save for NMT and judge engines.
    #   qe_autofix_max_rounds: fixer rounds, refused outside 1..5 (default 3).
    #   qe_autofix_cross_check: also score each version with the fixer LLM as a second
    #     opinion shown to the fixer (default False).
    qe_autofix: bool = False
    qe_autofix_provider: str | None = None
    qe_autofix_max_rounds: int = 3
    qe_autofix_cross_check: bool = False
    # --- LQA step configurable pass threshold (feat/lqa-configurable-threshold) ----
    # The numeric pass bar for an LQA step's gate. NULLABLE TRISTATE-ish: NULL (the
    # default, and every legacy row) = the built-in default (score >= 90 AND no
    # major/critical error) — byte-identical to the pre-configurable gate. A set value
    # (0-100) makes the step PASS only at score >= that value (the no-major-error policy
    # is UNCHANGED — only the numeric bar is configurable). This is DISTINCT from
    # qe_threshold: qe_threshold is the QE ROUTING cutoff on an llm_agent step (route
    # produced segments below it to review, default 70) and is DEAD on an lqa step;
    # lqa_threshold is the LQA reviewer PASS bar and is live ONLY on lqa steps. Resolved
    # by lqa._score / lqa.score_text (NULL -> lqa.LQA_PASS_THRESHOLD). Clamped 0-100 at
    # write time (workflows._clamp_lqa_threshold). Mirrors the app/db.py shim + the
    # alembic revision (nullability-parity CI guard).
    lqa_threshold: int | None = Field(default=None)
    # --- TM few-shot examples (SPEC-context-metadata phase 2B) -----------------
    # Render the top-N FUZZY TM matches for each segment as fenced example pairs in
    # this step's prompt (research: TM-few-shot is the #1 evidenced quality lever;
    # returns concentrate in the first 3-5 matches, and one bad match is worse than
    # none — so retrieval is relevance-gated at a similarity floor and SKIPS rather
    # than pads). Default ON (best-evidenced lever; gated safe by the floor + budget
    # cap), so a step with qualifying fuzzy matches WILL inject examples; a step with
    # NO qualifying match (or tm_examples OFF / window 0) renders NOTHING and the
    # prompt is byte-identical to pre-2B.
    #   tm_examples: master toggle for this step (default True).
    #   tm_examples_window: how many top matches to inject, clamped 0-5 (default 3;
    #     0 disables just like the toggle).
    tm_examples: bool = True
    tm_examples_window: int = 3
    # --- Per-step image context toggle (Batch 14 T2, SPEC §25) -----------------
    # llm_agent-only visual-context gate. DEFAULT TRUE = "this agent step sees the
    # string's attached images when the project engine is vision-capable"; FALSE opts
    # THIS step out (for example, keep it on the translator and off on a finalizer).
    # NOT NULL (a plain bool, exactly like qe_gate/tm_examples), so legacy rows
    # backfill to TRUE on upgrade — preserving the ratified "every LLM_AGENT step sees
    # the image" default until an author trims it. Inert on non-LLM_AGENT step types
    # (the orchestrator eligibility predicate excludes them by type regardless of this
    # flag). Mirrors the app/db.py shim + the alembic revision (nullability-parity CI
    # guard). See orchestrator._image_eligible_step.
    use_image_context: bool = True
    # --- Per-step concept-glossary renderer toggle (Terminology backend T3, SPEC
    # §9-T3 / LAW-TERM-14) ---------------------------------------------------------
    # llm_agent-only glossary-render swap, DEFAULT OFF (the only state T1/T2 shipped).
    # OFF -> the prompt's {glossary} block is prompts.glossary_line verbatim and the
    # gates read the full match set (byte-identical to main, LAW-TERM-1). ON -> the
    # block is glossary_concepts.render_for_llm's audience-gated, ordered, capped
    # Markdown, and the enforced set IS the shown set (LAW-TERM-15). The two caps bound
    # the rendered block server-side (LAW-TERM-13): glossary_concept_cap = at most N
    # concepts (default 10); glossary_token_cap = the block's token ceiling (0 = fall
    # back to install settings.DEFAULTS["glossary_prompt_token_cap"]="1500"). NOT NULL /
    # server-defaulted (a plain bool/int like use_image_context), so legacy rows backfill
    # to OFF/10/0 on upgrade. Inert on non-LLM_AGENT step types (they render no glossary
    # block). Mirrors the app/db.py shim + the alembic revision (nullability-parity CI
    # guard).
    concept_glossary_render: bool = False
    glossary_concept_cap: int = 10
    glossary_token_cap: int = 0
    # --- tm_match step per-step policy config (Batch 15 T1, SPEC-tm-match-step-config
    # §5.1) -----------------------------------------------------------------------
    # SIX nullable-inherit overrides, LIVE only on a StepType.TM_MATCH step (dead on
    # every other type via workflows._TM_MATCH_CONFIG_FIELDS). Follows the tm_protect
    # NULLABLE-TRISTATE precedent (NULL = inherit the live global, a concrete value =
    # per-step override), NOT the NOT-NULL use_image_context shape: every existing row
    # backfills to NULL = today's exact global-tracking behavior byte-for-byte
    # (LAW-TM-3). A NEW tm_match step is SEEDED with a concrete snapshot of the current
    # globals at creation (add_step, D15-4/LAW-TM-8); the pool selection is NEVER
    # snapshotted (S2 — pool membership is dynamic). Resolved by
    # tm_lock.resolve_policy_for_step. These names ECHO the like-named GLOBAL settings
    # keys (tm_lock_ice / tm_hundreds_enter_lqa / tm_context_floor) but live in a
    # different namespace (column vs setting); NOT the LLM_AGENT few-shot controls
    # (tm_examples / tm_examples_window / tm_protect), which are a different feature.
    tm_fill_threshold: int | None = Field(
        default=None
    )  # min match % to FILL; NULL->global; clamp [50,100]
    tm_fuzzy_lock_mode: str | None = Field(default=None)  # "none"|"soft"|"policy"; NULL->global
    tm_lock_ice: bool | None = Field(default=None)  # override; NULL->global tm_lock_ice
    tm_hundreds_enter_lqa: bool | None = Field(
        default=None
    )  # override; NULL->global tm_hundreds_enter_lqa
    tm_context_floor: int | None = Field(
        default=None
    )  # override; NULL->global tm_context_floor; clamp [50,99]
    tm_pool_selection: list | None = Field(
        default=None, sa_column=Column(JSON)
    )  # JSON list of readable project ids; NULL/[] -> all readable pools
    # --- over-translation (page-dump) guard opt-out (run-190 Item D / M5) --------
    # TRISTATE. NULL (the default, and every legacy row) = follow the global
    # OVER_TRANSLATION_MAX_RATIO knob (guard ON by default); False = this llm_agent step
    # opts OUT of the page-dump guard (a legitimately EXPANDING rewrite / post-edit /
    # summarize step whose target is far longer than its source, which would otherwise trip
    # `too_long` and be withheld to NEEDS_REVIEW); True = explicitly ON. llm_agent-only
    # (inert on tm_match / lqa / human_review, like tm_protect). Resolved by
    # orchestrator._over_translation_guard_on(step); alembic revision b7e3f1a9c2d4.
    over_translation_guard: bool | None = Field(default=None)
    # --- TM provenance guard (PROPOSAL-lqa-glossary-adjudication Phase 1, D1) --
    # TRISTATE. NULL (the default, and every legacy row) = the CORRECTION-LOOP-shaped
    # default: this step excludes `tm_origin` segments from its pending set ONLY when it
    # is a fix step (gated on `prev_lqa:failed`, or the target of an LQA `on_fail`
    # branch). True = ALWAYS protect (pin a polish step off the TM-filled segments);
    # False = NEVER protect (unprotect a fixer that is deliberately meant to rewrite TM
    # output). Deliberately narrow by default: protecting TM segments from every later
    # step would change the semantics of every existing "Translator -> In-country
    # reviewer" chain. A workflow with no LQA step has no correction-loop-shaped step,
    # so NULL resolves to "no protection" everywhere and the run is byte-identical to
    # pre-Phase-1. Resolved by lqa_safety.tm_protect_for(); kill switch
    # `tm_fix_protection_enabled`. llm_agent-only (dead on lqa/human_review).
    tm_protect: bool | None = Field(default=None)
    # --- conditional-workflow PRE-STEP GUARD (SPEC-conditional-workflows §3.1 / §5,
    # Phase 2) -----------------------------------------------------------------
    # An optional structured guard: run this step only if the condition holds; else
    # SKIP it (the rolling text passes through unchanged). NULL (the default) = ALWAYS
    # run -> byte-identical to today (a plain step never reaches the evaluator). Shape
    # (validated against an allowlist, NEVER eval'd -- context_pkg ethos):
    #   {"ref": "prev_lqa"|"prev_qe", "op": "passed"|"failed"|"score_gte"|"score_lt",
    #    "value": <number|null>}
    # `ref` names the immediately-prior outcome-producing step (the most recent LQA for
    # prev_lqa, the most recent QE-gated step for prev_qe -- read from the durable
    # Phase-1 StepRun.gate/score, so evaluation survives pause/resume). `passed`/
    # `failed` read `gate`; `score_gte`/`score_lt` compare `score` to a numeric `value`.
    # Save-time validation (wf_service) rejects an unknown ref/op, a score op with no
    # numeric value, extra keys, or a ref that does not RESOLVE to an earlier step of
    # the right type -- so a malformed guard can never reach a run. Whole-document only
    # (Per the maintainer); no named-step refs / branching / loops this phase (Phase 3).
    run_condition: dict | None = Field(default=None, sa_column=Column(JSON))
    # --- conditional-workflow BRANCHING + bounded loop-back (SPEC §3.1 / §5, Phase 3;
    # Maintainer decisions D1/D3/D4) ------------------------------------------------
    # `key` is a human-authorable stable slug, UNIQUE within a workflow (D1). Branch
    # targets and (future) step:<ref> conditions name a step by this key, so the graph
    # survives reordering/rename (order_index renumbers, names aren't unique). NULL (the
    # default) = an unnamed step -> byte-identical to today; a step is only a branch
    # TARGET if it carries a key. Slugified + uniqueness-validated at save time.
    key: str | None = Field(default=None)
    # `branch` is post-step routing, only meaningful on a step that PRODUCES A GATE
    # (an LQA step, or a qe_gate=True llm_agent step -- D3 rejects a branch on any
    # other step type at save time). NULL (default) = fall through to the next step
    # (byte-identical). Shape (validated against an allowlist, NEVER eval'd):
    #   {"on_pass": <target>, "on_fail": <target>}
    # where target is "next" (cursor+1) | "end" (finish the run) | {"step": "<key>"}
    # (jump to that step; an EARLIER key = a bounded loop-back). The step's durable
    # Phase-1 gate ("passed"/"failed") picks on_pass vs on_fail at the loop seam.
    branch: dict | None = Field(default=None, sa_column=Column(JSON))
    # `max_visits` is this step's per-run execution ceiling for loop safety (D4). NULL
    # = inherit the run-level default (settings `workflow_max_visits`, default 5). When
    # a loop-back would revisit this step past the ceiling, the branch seam refuses the
    # jump and falls through (the loop ENDS gracefully -> the run COMPLETES, flagged;
    # NOT a hard fail). The run-level total-step cap (settings `workflow_total_step_cap`,
    # default 50) is the absolute backstop -> FAILED "loop_cap_reached". Inert on a
    # linear run (each step visited once, well under any cap).
    max_visits: int | None = Field(default=None)
    # --- LLMemory step-level opt-out (PROPOSAL-llmemory §6 "WorkflowStep.memory_mode
    # tristate (auto | off)") ---------------------------------------------------
    # NULLABLE TRISTATE-BY-STRING. NULL (the default, and every legacy row) and "auto"
    # both mean "this step participates in memory recall normally"; "off" means NO
    # memory block is ever injected into this step's prompts, whatever the global
    # switches say. NULL and "auto" are deliberately the SAME state rather than NULL
    # meaning "inherit something": there is nothing to inherit from in Phase 1, and a
    # two-value semantic with three spellings is how a tristate quietly grows a third
    # behaviour nobody designed.
    #
    # This is an OPT-OUT ONLY. It can never turn recall ON for a step that is not
    # eligible: eligibility (a translate step, never an LQA/judge step, never the
    # correction-loop fixer -- proposal §2 "no recall into LQA judge or fixer") is
    # decided by the orchestrator's own step-role predicate, and memory_mode is
    # consulted AFTER that gate, only ever to say no. So "auto" on an LQA step is
    # inert, exactly like tm_protect is dead on a non-llm_agent step.
    #
    # Existing rows backfill NULL -> byte-identical to pre-LLMemory. Mirrors the
    # alembic revision f2a9c4d81b73 (nullability-parity CI guard) and the app/db.py
    # `_ADDED_COLUMNS` shim (a new COLUMN on an EXISTING table, which create_all()
    # would never add to a live SQLite file).
    memory_mode: str | None = Field(default=None)
    # --- parallel step routing (SPEC-parallel-routing §5.6, Phase 1) -----------
    # An ANNOTATION over the order_index spine, NOT a DAG edge. Steps that are
    # CONSECUTIVE in order_index and share a non-NULL `parallel_group` slug form a
    # PARALLEL GROUP: execution reaches the first member and FORKS -- every member runs
    # concurrently against the SAME frozen snapshot of the working text -- then JOINS
    # (the next step starts only when every member has finished). NULL (the default,
    # and every legacy row) = not in a group -> byte-identical to today (a workflow with
    # no groups never forks; `_has_parallel` gates the whole seam off).
    #
    # PHASE 1 IS CHECK-ONLY (§5.1.2): members must be `lqa` steps. Lanes only READ the
    # working text, so there is no merge semantics to invent and the "segments are
    # written exactly once, post-loop, on success" choke point is untouched. An
    # `llm_agent` member is REFUSED at save time (not silently cleared -- ignoring it
    # would change execution shape); on `human_review` it is a dead field.
    #
    # The slug is authored like `key` (same slugify_key rules) but is NOT unique per
    # workflow -- it is shared BY definition (that is what makes the members a group).
    # Mirrors the alembic revision a4e7d2c8b91f + the app/db.py `_ADDED_COLUMNS` shim
    # (a new COLUMN on an EXISTING table, which create_all() would never add to a live
    # SQLite file) -- the nullability-parity CI guard enforces the three agree.
    parallel_group: str | None = Field(default=None)
    # --- external/vendor dispatch-and-wait (PROPOSAL-external-processing-step Wave A) --
    # `vendor_id`: the Vendor a StepType.EXTERNAL step dispatches to (decision 6). NULL
    # on every legacy row and every non-external step -> byte-identical (nothing reads
    # it off a non-external step, exactly as glossary_id is inert on a non-llm step).
    # Consumed ONLY by the EXTERNAL branch in _execute_step, which validates it FAIL-
    # CLOSED against the document's customer's linked vendors before dispatch. A plain
    # INTEGER in the shim (SQLite cannot add a REFERENCES clause to an existing table
    # without a rebuild, same as key_id/glossary_id); a real FK in the model + migration.
    vendor_id: int | None = Field(default=None, foreign_key="vendor.id")
    # `external_wait_hours`: the bounded-wait window for a parked external leg before it
    # escalates to a human_review pause (decision 3 — expiry ALWAYS escalates, never
    # hard-fails). NULL = the install default (settings `vendor_dispatch_ttl_hours`).
    # Inert on any non-external step. Mirrors the model + the alembic migration +
    # the db._ADDED_COLUMNS shim (nullability-parity CI guard).
    external_wait_hours: int | None = Field(default=None)


class BatchStatus:
    """Rollup status of a WorkflowBatch (SPEC-workflows-redesign Phase 5a). Distinct
    from RunStatus because a batch aggregates N member runs, not one execution:

      RUNNING   — the dispatcher is still launching members, or at least one member
                  is not yet terminal. The STORED authority for "terminal" is
                  WorkflowBatch.finished_at (see batch_runs.batch_status): while it is
                  NULL the batch reads RUNNING; once stamped, the stored status below is
                  the authoritative terminal record.
      COMPLETED — every requested locale launched a member and every member COMPLETED.
      PARTIAL   — a mix: some members failed/paused, OR the dispatcher stopped launching
                  early (spend cap tripped / fail-fast after a member failed / a locale
                  skipped as already-running), so coverage < requested.
      FAILED    — members launched and every one of them FAILED (no completion at all).
    """

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class WorkflowBatch(SQLModel, table=True):
    """One fan-out of a workflow across a document's target locales as a single
    observable, spend-capped, concurrency-bounded unit (SPEC-workflows-redesign
    Phase 5a — the batch layer over the per-(workflow, document, locale) single run).

    Each member run is an ordinary WorkflowRun carrying batch_id -> this row; a NULL
    batch_id is a plain single run, byte-identical to every pre-5a path (nothing legacy
    ever sets a batch_id). requested_locales is the EXACT target set of THIS batch, so
    un-launched locales (spend-stopped / fail-fast-stopped / skipped-as-already-running)
    are visible as requested_locales minus the member runs. status is a rollup (see
    BatchStatus); finished_at is the terminal authority (batch_runs.batch_status reads
    RUNNING until it is stamped, so a finished batch never shows a stale 'running')."""

    id: int | None = Field(default=None, primary_key=True)
    # Org isolation (multi-org): resolved from the document's project at dispatch, like
    # every root-owned row. NULL single-tenant (gate off) = byte-identical to today.
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # The REQUESTED workflow. Members resolve a per-locale variant off this via
    # workflows.resolve_variant, so member WorkflowRun.workflow_id may differ per locale
    # (an ungrouped workflow resolves to itself — the sacred legacy path).
    workflow_id: int = Field(foreign_key="workflow.id")
    document_id: int = Field(foreign_key="document.id")
    created_by: str = ""  # email/pseudo-actor that launched the batch (attribution)
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None  # terminal authority (see BatchStatus.RUNNING)
    # The exact target locales of THIS batch, as a JSON list (stored TEXT on SQLite).
    # requested_locales minus the member runs = the locales that never launched.
    requested_locales: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # Selected locale -> last member id BEFORE the accepted re-run. Only newer members release.
    reopened_members: dict | None = Field(default=None, sa_column=Column(JSON))
    # Persisted before dispatch; NULL means legacy/unknown ownership, unsafe for manual recovery.
    dispatcher_owner: dict | None = Field(default=None, sa_column=Column(JSON))
    # Run-level concurrency ceiling (clamped 1-8; default from the batch_max_parallel
    # setting). Multiplies with per-segment mt_concurrency — see batch_runs for the
    # worst-case note; both are clamped conservatively.
    max_parallel: int = 3
    # Stop LAUNCHING further locales on the first member FAILED (in-flight members are
    # NOT killed — no cancellation hook exists; the honest limit is noted in `notes`).
    fail_fast: bool = False
    status: str = BatchStatus.RUNNING
    # Human-readable explanation of an early stop (spend cap tripped / fail-fast / which
    # locales were skipped or never launched). NULL when the batch ran clean to coverage.
    notes: str | None = None


class WorkflowRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    workflow_id: int = Field(foreign_key="workflow.id")
    document_id: int = Field(foreign_key="document.id")
    # --- fan-out batch membership (SPEC-workflows-redesign Phase 5a) ---
    # NULL = a plain single run (byte-identical to every pre-5a path; nothing legacy
    # ever sets it). Non-NULL links this run to the WorkflowBatch that dispatched it as
    # one member (one member run per requested locale). Additive nullable column.
    batch_id: int | None = Field(default=None, foreign_key="workflowbatch.id")
    target_locale: str = ""  # the locale the run was launched for; needed so the
    # run view + resume use the RIGHT locale on a
    # multi-target document (not the first one found)
    status: str = RunStatus.PENDING
    current_step_index: int = 0
    # Attribution (jobs-history): the email/pseudo-actor that LAUNCHED this run, so
    # the unified /jobs history log can show who ran a workflow (a run never had an
    # actor column before — the history page showed "—"). Threaded through every
    # run-creation seam (web start_run, CLI run_workflow, the stream fns); a resume
    # does NOT re-stamp it (the launcher stays the owner). "" for pre-existing rows
    # and any seam that supplies no actor -> renders "—", byte-identical to before.
    created_by: str = ""
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    # --- conditional-workflow per-run scratchpad (SPEC-conditional-workflows §3.3,
    # Phase 1) -----------------------------------------------------------------
    # A durable per-run scratchpad that SURVIVES pause/resume (unlike the in-memory
    # tallies the generator loses when it exits on a human pause). Phase 2/3 store
    # {"visits": {stepRef: count}, "total_steps_run": int, "last_outcomes": {...}}
    # here and read it at the loop seam to evaluate guards/branches. NULL until the
    # FIRST conditional feature writes it — a plain (non-conditional) run NEVER writes
    # it, so every existing run + every plain run stays byte-identical (context IS
    # NULL). Read by nobody this phase.
    context: dict | None = Field(default=None, sa_column=Column(JSON))
    # --- why this run ended the way it did, in words (GitHub #44) --------------
    # A run had NO place to record a reason, so a run failed by something OUTSIDE
    # the step machinery (a server restart that killed the executing thread, a
    # staleness sweep) read as a bare FAILED with no StepRun error to explain it —
    # indistinguishable from "never started". WorkflowBatch has carried `notes` for
    # exactly this since Phase 5a; this is the single-run half.
    #
    # NULL on every existing row and on every ordinary run (nothing on the happy
    # path writes it), so all pre-existing rows and the whole normal pipeline are
    # byte-identical. Written today only by the restart/staleness recovery seam
    # (`orchestrator.recover_interrupted_runs` and `start_run`'s staleness floor).
    notes: str | None = None
    # --- durable tm_match provenance marker (Batch 15 T1, SPEC-tm-match-step-config
    # §5.1b / N2) -----------------------------------------------------------------
    # A monotonic per-run fact: True once THIS run EXECUTED a tm_match step (in EITHER
    # the segmented OR the FULL_TEXT branch — P2), even if it filled nothing. The T2
    # delivery empty gate reads it to decide "did some run for this deliverable actually
    # run tm_match" WITHOUT a mutable WorkflowRun->Workflow->WorkflowStep.step_type join
    # (which a builder's delete_step/update_step could flip, silently un-holding or
    # retro-holding deliverables — the defect class N2 closes). NOT NULL, server_default
    # '0' (the qe_gate DEFAULT-FALSE bool shape, NOT the nullable-inherit tm_* shape):
    # every legacy run backfills False, so a pre-Batch-15 tm_match run is NOT
    # retroactively gated (the conservative, prospective upgrade posture, §11/P5). Never
    # un-stamped once True. Mirrors the app/db.py shim + the alembic revision
    # (nullability-parity CI guard).
    used_tm_match: bool = False
    # --- IN-STEP PROGRESS (GitHub #101) ----------------------------------------
    # A classic run over a multi-segment document showed NO movement on ANY surface for
    # the whole of its `llm_agent` step -- 10-25 minutes on a local engine -- because a
    # StepRun row is only written when the step ENDS: `/api/runs/{id}` reported
    # `step_states: []`, the document's `translated` count stayed 0, `workflow show-run`
    # mirrored the API, and `/runs/stream` (observe_run polls StepRun rows) streamed
    # silence. The only live signal was the default-OFF LLM Ledger, and on a FULL_TEXT run
    # even that ticks once per ~6-minute call. So a monitor could not tell a working run
    # from a wedged one, and this campaign's own harness raised false "stuck run" alarms.
    #
    # These SIX columns are that missing signal: the orchestrator counts "N of M units
    # done" from INSIDE the long step (per segment as the fan-out completes, per chunk on
    # the FULL_TEXT path), and `/api/runs/{id}`, `workflow show-run` and the SSE stream all
    # read it. `progress_at` is the liveness half -- a counter that has not moved in an hour
    # says something different from one that ticked ten seconds ago.
    #
    # THE COUNTER LIVES IN MEMORY; THESE COLUMNS ARE ITS COARSE PUBLICATION, AND NO
    # TRANSLATION THREAD EVER WRITES THEM MID-STEP. A tick only bumps an in-process map
    # (a fan-out worker may never open a Session -- that is the #100 pattern, QueuePool
    # exhaustion); the run's own thread then just marks the run id dirty, and ONE daemon
    # writer thread does the UPDATE, skipping it outright when the pool has no free slot.
    # At a step boundary nothing extra is opened at all: the final count is stamped on the
    # run's own ORM object and rides that step's commit. So a progress write can never
    # fail, slow or alter a translation -- an earlier cut opened a Session inline and
    # blocked the translation thread 30 s per flush under #100's pool exhaustion -- and a
    # lost one costs a stale counter for a few seconds. 0/0/""/NULL on every existing row
    # and on any run that never publishes, which the surfaces report as "no progress
    # recorded" rather than a false 0%. Mirrors the app/db.py workflowrun shim + alembic
    # revision d1f4a7c3e9b2 (nullability-parity CI guard).
    #
    # THE COUNTER IS PER STEP, AND SAYS WHICH. Each long step publishes a fresh 0-of-M, so a
    # bare `progress_done` legitimately DECREASES at a step boundary and `progress_unit`
    # changes under it (segments -> chunks). `progress_step` / `progress_step_index` name the
    # step the current N-of-M belongs to, so a monitor watching for "the counter went
    # backwards" can tell a step transition from a regression. "" / 0 when nothing published.
    progress_done: int = 0
    progress_total: int = 0
    progress_unit: str = ""  # "segments" | "chunks" | "" (nothing published)
    progress_step: str = ""  # the step's name (or its type), "" when nothing published
    progress_step_index: int = 0  # that step's order_index in the workflow
    # The MT formality register that applied to this run (#617 item 7, ADR 0062):
    # {register, source, target_locale, label[, applied]}, stamped when the run starts. NULL =
    # none resolved (no instruction) and every pre-existing run.
    formality: dict | None = Field(default=None, sa_column=Column(JSON))
    progress_at: datetime | None = None
    # --- fan-out queue membership (gate A round 3, HIGH-1/2/3) -----------------
    # NULL = an ordinary single launch (byte-identical to every path that existed
    # before). Non-NULL names the FAN-OUT this row was created by — one opaque token
    # per "translate all" click / per `keys translate` call — and it is what makes the
    # queue honest on three axes:
    #   * the launch response and the audit row can name exactly which run rows this
    #     action queued, so a partial launch is never reported as a whole one;
    #   * the admission scheduler serves fan-outs ROUND-ROBIN by this token, so one
    #     72-run "translate all" cannot starve a second operator's fan-out;
    #   * a `wait=True` CLI drain can find its own queued rows again after a crash.
    # Written once at row creation and never changed (an admitted row keeps its token,
    # which is how the group stays greppable after the fact). Additive + nullable, so
    # every existing row and every non-fan-out launch is byte-identical. Mirrors the
    # app/db.py workflowrun shim + alembic revision e2b6c9f4a713 (nullability-parity CI).
    queue_group: str | None = None


class StepRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    workflow_run_id: int = Field(foreign_key="workflowrun.id")
    step_id: int = Field(foreign_key="workflowstep.id")
    # `index=True` (SPEC-wave3-collaboration §8.5): the reviewer worklist filters
    # paused steps on status, and StepRun declared NO indexes at all before Wave 3.
    status: str = Field(default=RunStatus.PENDING, index=True)
    input_text: str = ""
    output_text: str = ""
    provider_used: str | None = None
    model_used: str | None = None
    attempts: int = 0
    error: str | None = None
    latency_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # --- conditional-workflow durable outcome signal (SPEC-conditional-workflows
    # §3.2, Phase 1) -----------------------------------------------------------
    # The structured, queryable outcome a Phase 2/3 condition reads (today LQA score
    # is a stringified `error` and QE score is in-memory + Segment.qa_flags — lost on
    # pause/resume). WRITTEN now (LQA + QE-gate branches populate score/gate); READ BY
    # NOBODY until Phase 2/3. A step that produces no outcome leaves score/gate NULL.
    # outcome_index is this step's visit number in this run (always 0 this phase — no
    # loops yet). Additive + neutral-defaulted so an existing StepRun row (and every
    # plain run) is byte-identical.
    score: float | None = None  # LQA score / run-level QE aggregate; NULL = none
    gate: str | None = None  # "passed" | "failed" | NULL — canonical pass/fail
    outcome_index: int = 0  # visit number of this step in this run (0 = first)
    # --- real per-step-visit spend (feat/workflow-spend) -----------------------
    # The ACTUAL USD cost + token usage accumulated across EVERY completion call this
    # step-visit made (per-segment fan-out + QE-judge calls included). `cost_usd` is
    # the SAME figure the spend ledger accrued for those calls — it comes from
    # run_completion, which returns the exact cost spend.record_usage added (0.0 for a
    # MockProvider/LocalProvider-exempt call, honest). The reports `--group-by workflow`
    # axis sums these across a run's StepRuns to attribute EXACT per-workflow spend
    # (replacing the old most-recent-WorkflowRun job-cost approximation).
    #
    # NULL is HONEST "pre-tracking / unknown": every StepRun written before this
    # feature reads NULL (legacy), and the reports axis buckets a run whose StepRuns
    # are ALL NULL as `(untracked)` rather than silently zeroing it. A NEW run stamps
    # real values on the completion-bearing steps (llm_agent / QE / LQA); steps that
    # spend nothing (human_review / skipped / cap-halted) leave these NULL (no call
    # was made — honest, not a zero). tokens_in = input+cache tokens fed in; tokens_out
    # = output tokens. All three are additive + nullable, so an existing StepRun row
    # (and every plain run) is byte-identical.
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    # --- per-step FAILURE SHAPE (run 190 Item C) -------------------------------
    # The structured half of the free-text `error` column, so the run view / GET
    # /api/runs/{id} can surface the SHAPE of a failure (attempts, provider, model,
    # finish reason, provider error class, output length, elapsed) as METADATA — no
    # prompt/response text — safe on every surface incl. --remote. Threaded up from
    # run_completion's result dict; stamped on the completion-bearing step visits.
    #   finish_reason  — the terminal provider finish reason ("length" for the run-190
    #                    thinking burn, "stop" on a clean success), or NULL.
    #   error_class    — the terminal exception TYPE NAME ("ThinkingBurn",
    #                    "ProviderTruncated", "UnderTranslationError"), or NULL on success.
    #   output_chars   — characters of translation text produced (0 on the empty-output
    #                    thinking burn), or NULL when not tracked.
    # All three NULLABLE, no default — NULL is the honest "pre-tracking / unknown" state
    # (every legacy StepRun reads NULL, no backfill). Mirrors app/db.py's steprun shim +
    # the alembic revision b7e3f1a9c2d4 (nullability-parity CI).
    finish_reason: str | None = None
    error_class: str | None = None
    output_chars: int | None = None
    # --- #356 phase 2a: which system-prompt HEAD this step visit ran with ----------------
    # Stamped ONLY on an AI step visit while the `system_prompt_overrides` flag is ON (flag
    # off writes nothing, so a plain install's rows are byte-identical): the editable key
    # ("step_head"), the override VERSION in force (0 = the built-in default) and the sha256
    # of the COMPOSED step system text (head + language pair + marker clause, plain-text
    # variant for the run's language pair). Never the prompt body. NULL = flag off / legacy.
    system_prompt_key: str | None = None
    system_prompt_version: int | None = None
    system_prompt_sha256: str | None = None
    # #356 phase 2b: which scope's head won ("workflow" | "org" | "builtin"); NULL on rows
    # stamped by phase 2a (read there as org when version > 0, else builtin).
    system_prompt_scope: str | None = None


class DeliveryEmptyApproval(SQLModel, table=True):
    """A human's audited authorization to DELIVER a tm_match-provenanced deliverable that
    still carries EMPTY (untranslated) targets (Batch 15 T2, SPEC-tm-match-step-config
    §5.2 / D15-6). The record carries ONLY the human APPROVAL; the held state itself is
    DERIVED LIVE at delivery time (`tm_delivery.empty_gate_decision`), never armed at run
    terminal -- so there is NO "armed" row, and an unapproved empty-held target simply has
    no record here.

    DUAL-MODE (N1), separated by ``document_id``:
      * DOCUMENT mode -- ``document_id`` set, keyed on ``(document_id, locale)``: a single
        document project's deliverable.
      * KEY / PROJECT mode -- ``document_id IS NULL``, keyed on ``(project_id, locale)``:
        the whole-project key export (`key_files.export_locale`), which spans MANY internal
        key documents (fmt=="keys", one per namespace) with no single ``Document`` to key on.
    ``document_id`` is NULLABLE and a reader MUST NOT assume it non-null anywhere.

    The two PARTIAL unique indexes keep "one live approval per delivery target" for each
    mode WITHOUT colliding (a single composite unique over a nullable column is defeated by
    SQL's NULL-distinctness on SQLite -- N1). They are declared HERE (not only in the
    migration) so ``create_all`` emits them, with BOTH dialect predicates -- the
    models.py precedents ``ux_stringkey_identity`` / ``ux_keyimage_key_ordinal``. Neither
    ``verify_alembic_authority`` (no index diff) nor ``verify_migrations`` (only the four
    db.py hot-path indexes) compares these, so an explicit three-path index+WHERE probe in
    ``verify_tm_empty_delivery.py`` covers them (P4).

    ``covered`` is MODE-TYPED + self-describing (P12): ``{"mode": "doc"|"keys", "ids":
    [...]}`` -- doc-mode stores the SEGMENT ids the approver saw empty, key-mode stores the
    STRING-KEY ids. A record COVERS a live delivery iff ``status=="approved"`` AND its
    ``covered["mode"]`` matches the delivery mode AND the CURRENT empty identity set is a
    SUBSET of ``covered["ids"]`` (LAW-TM-15 growth guard: a later run that GROWS the empty
    set re-holds until re-approved; a shrink stays covered). It stores tenant-scoped ids +
    the empty identity set only -- never source text (data-export/GDPR note, P3)."""

    __table_args__ = (
        # DOCUMENT-mode: one live approval per (document, locale). WHERE document_id IS NOT
        # NULL so key-mode rows (document_id NULL) are not indexed by this one at all.
        Index(
            "ux_dea_doc",
            "document_id",
            "locale",
            unique=True,
            sqlite_where=text("document_id IS NOT NULL"),
            postgresql_where=text("document_id IS NOT NULL"),
        ),
        # KEY/PROJECT-mode: one live approval per (project, locale) among the document_id
        # NULL rows. NULLs are DISTINCT in a SQL unique index, so without this partial
        # WHERE a plain (project_id, locale) unique would never fire for key-mode rows.
        Index(
            "ux_dea_keys",
            "project_id",
            "locale",
            unique=True,
            sqlite_where=text("document_id IS NULL"),
            postgresql_where=text("document_id IS NULL"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    # Tenant anchor + KEY-mode key. NOT NULL in BOTH modes -- the data_export Extractor +
    # the GDPR/cascade reap anchor on this (P3), never on the nullable document_id.
    project_id: int = Field(foreign_key="project.id", index=True)
    # NULL = KEY/project mode; set = DOCUMENT mode. NULLABLE (N1).
    document_id: int | None = Field(default=None, foreign_key="document.id", index=True)
    locale: str = Field(index=True)
    # The run in flight when the approval was recorded -- informational provenance only
    # (the gate keys on the DURABLE WorkflowRun.used_tm_match marker, not this).
    run_id: int | None = Field(default=None, foreign_key="workflowrun.id")
    # {"mode": "doc"|"keys", "ids": [...]} -- the empty identity set the approver saw (P12).
    covered: dict | None = Field(default=None, sa_column=Column(JSON))
    covered_count: int = 0  # |covered ids| at approval (info / display)
    status: str = "approved"  # "approved" | "revoked"
    approved_by: str = ""  # the approving human's email (GDPR-swept, P3)
    approved_at: datetime = Field(default_factory=_utcnow)


# --- reusable library ---
class PromptTemplate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    scope: str = Scope.GLOBAL
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    body: str = ""
    # Multi-org P8 (pre-gate cleanup): org_id closes the cross-org leak flagged in
    # the plan — prompt templates were app-global, so one customer's saved prompts
    # would surface to another under the sacred gate. Nullable; _ensure_default_org
    # backfills existing rows onto the Default org. Stamped at create; list-scoped
    # by /templates. Inert single-tenant (NULL, no filtering) — the gate governs it.
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_at: datetime = Field(default_factory=_utcnow)


class SystemPromptOverride(SQLModel, table=True):
    """One saved version of an org's override of an EDITABLE pipeline system prompt head
    (GitHub #356 phase 2a). Append-only history: every save, restore and reset adds a row
    with the next `version` for (org, prompt_key); rows are never edited or deleted. The
    CURRENT state of a key is its highest-version row: `action` "set" / "restore" means
    `body` is the live head, "reset" means the built-in default is back in force (body "").
    `org_id` NULL = the single-tenant install (adopted by the Default org if multi-org is
    ever enabled). `prompt_key` is restricted in code to the editable allow-list; the locked
    floor (marker clauses, language pair, content fence, output contract) is never stored.
    `sha256` is of `body`; `created_by` is the acting admin's email or the CLI actor."""

    __table_args__ = (
        Index(
            "ux_systempromptoverride_version",
            text("coalesce(org_id, -1)"),
            text("prompt_key"),
            text("version"),
            unique=True,
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    prompt_key: str
    version: int = 1
    action: str = "set"  # set | restore | reset
    body: str = ""
    sha256: str = ""
    restored_from: int | None = None  # the version a "restore" copied
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class WorkflowSystemPromptOverride(SQLModel, table=True):
    """One saved version of a WORKFLOW's own override of an editable pipeline system prompt
    head (GitHub #356 phase 2b). Same append-only shape as `SystemPromptOverride` (the
    org-level history): every save, restore and reset adds the next `version` for
    (workflow, prompt_key); the live state is the highest-version row, and a "reset" row means
    the workflow INHERITS the org override (or the built-in text). A sibling table rather than
    a scope column on the org table: the org table's unique index and every org query stay
    untouched, and a workflow delete reaps exactly its own rows (a plain FK, no NULL-able scope
    to special-case). The org is never stored here: it is the workflow's own `org_id`, so a
    row can never disagree with the workflow it belongs to. `body` is the plain head text
    (the locked floor is never stored); `sha256` is of `body`; `created_by` is the acting
    user's email or the CLI actor."""

    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "prompt_key", "version", name="ux_wfsystempromptoverride_version"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    workflow_id: int = Field(foreign_key="workflow.id", index=True)
    prompt_key: str
    version: int = 1
    action: str = "set"  # set | restore | reset
    body: str = ""
    sha256: str = ""
    restored_from: int | None = None  # the version a "restore" copied
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class EngineProfile(SQLModel, table=True):
    """A named, credentialed MT/LLM engine (P3, item 6). Turns the single global
    local-engine slot into a registry of engine cards, each addressable as a
    provider name "engine:<id>" (providers.get_provider resolves it to the right
    Provider subclass built from this row's base_url/key/model/config).

    `kind` discriminates which Provider subclass backs the engine:
      local             — OpenAI-compatible self-hosted server (LM Studio/Ollama/vLLM)
      openai_compatible — a hosted OpenAI-Chat-Completions endpoint reached with a BYO
                          bearer key (OpenRouter/DeepSeek/Mistral/Groq/Together/…);
                          base_url + model + api_key, reuses the local wire format
      deepl             — DeepL NMT (text-in/text-out; formality/model_type/context/
                          custom_instructions params)
      google            — Google Cloud Translation Basic v2 (simple API-key)
      microsoft         — Azure Translator Text v3.0 (subscription key + region)
      cohere            — Cohere v2/chat LLM (prompt-based, SSE streaming)
      lara              — Lara / Translated adaptive MT (NMT char-billed path; key+secret
                          PAIR auth, EXPERIMENTAL HMAC signing; style/instructions/
                          adapt_to/glossaries config)
      amazon            — Amazon Translate NMT (SigV4 auth: access key + secret in the
                          config blob + REQUIRED region; TranslateText JSON-RPC)
      jev / jev_local   : quality JUDGES, not translators (hosted TypeSafe Jev with a
                          pinned URL; a Jev-compatible server such as SemIf). Scored by
                          qe_jev only; config holds confidence_floor/meaning_floor/timeout_s
    (extensible: custommt + the NICE-tail providers are DEFERRED to a fast-follow per the
    plan. Lara/Amazon key+secret PAIRS keep the primary id/access-key in api_key_enc and
    the paired secret Fernet-encrypted in config.access_key_secret_enc.)

    `api_key_enc` is a Fernet ciphertext (never plaintext) under config.ENCRYPTION_KEY
    — the same secret pattern as connector credentials + settings.set_secret. It is
    written via engines.set_profile_key and never echoed in list/show/JSON.

    `config` is a per-kind extras blob: DeepL {formality: "more"|"less"|...},
    Microsoft {region: "eastus"}, etc. `enabled` gates whether the engine is offered
    in the default_provider / workflow-step pickers. `org_id` ships nullable from day
    one (multi-org gate is OFF; column exists so the gate needs no later migration)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # admin-chosen, unique per install
    kind: str = (
        "local"  # local | openai_compatible | deepl | google | microsoft | cohere | lara | amazon
        # | jev | jev_local (quality judges)
    )
    base_url: str = ""
    api_key_enc: str = ""  # Fernet ciphertext (never plaintext)
    model: str = ""
    config: dict | None = Field(default=None, sa_column=Column(JSON))
    enabled: bool = True
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


# --- quality ---
class Setting(SQLModel, table=True):
    """App-level key/value settings (org name, default provider/models, etc.)."""

    key: str = Field(primary_key=True)
    value: str = ""


# --- organizations (tenancy groundwork) ---
class Organization(SQLModel, table=True):
    """Single row per install today (seeded as 'Default'). Schema groundwork for a
    future SaaS/multi-tenant mode — org-scoped query enforcement is NOT implemented;
    the install itself is the tenant boundary for on-prem deployments."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = "Default"
    # Suspension flag (multi-org P7). A suspended org's users cannot log in and its
    # API tokens are rejected — but ONLY when the sacred gate is on. Default True
    # (active) so single-tenant installs are byte-identical: nothing reads it until
    # multi_org_enabled is turned on. access.org_active() is the single authority.
    # server_default="1" so a raw INSERT that omits `active` (db._ensure_default_org)
    # + existing rows on the additive migration both backfill to active, matching
    # the hand-written Alembic revision.
    active: bool = Field(default=True, sa_column_kwargs={"server_default": "1"})
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectMember(SQLModel, table=True):
    """Grants a non-privileged user visibility into a project. Admins, managers and
    linguists see every project; translators/reviewers/viewers see projects they
    created or are members of (when the project_scoping setting is on)."""

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    added_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ApiKey(SQLModel, table=True):
    """A per-user API credential (shown once at creation; only the hash is stored).
    Scopes: 'api' = the inbound token API. The legacy shared connector token remains
    valid ONLY for connector/companion-app endpoints."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    name: str = ""
    prefix: str = ""  # first chars of the key, for display
    key_hash: str = Field(index=True)  # sha256 of the full key
    scopes: str = "api"
    active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    last_used: datetime | None = None
    # Optional expiry (#270 r2-apiauth-10): NULL = never expires. Past-or-equal the
    # timestamp the key is refused exactly like a revoked one (verify_key). Every
    # pre-existing key keeps NULL = no expiry, so the column is purely additive.
    expires_at: datetime | None = None


class McpOAuthGrant(SQLModel, table=True):
    """One consented MCP OAuth grant (#753): a user approved a remote MCP client on the consent
    page of this install's opt-in authorization server (app/services/mcp_as.py).

    HASHES ONLY, like ApiKey: the authorization code, the MCP access token, the API token the
    MCP server exchanges it for and the refresh token are stored as SHA-256 digests; the raw
    values are shown once on the wire. One row carries the CURRENT token generation (a refresh
    rotates every hash; the previous refresh hash is kept only to detect reuse, which revokes
    the grant). `resource` is the audience the tokens are bound to (RFC 8707); `scopes` is a
    subset of api/docs/keys. `revoked_at` set = dead for good (revocation by the user, account
    disable via apikeys.revoke_all_for_user, code or refresh reuse). A NEW TABLE: create_all on
    SQLite, alembic revision a7c3e9f1b2d4 on the alembic path; EXPLICIT_EXCLUSIONS in
    data_export_part2 (credential material, no portable content)."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    client_id: str = ""
    client_name: str = ""
    redirect_uri: str = ""
    scopes: str = "api"
    resource: str = ""
    code_hash: str | None = Field(default=None, index=True)
    code_challenge: str = ""
    code_expires_at: datetime | None = None
    code_redeemed: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    access_hash: str | None = Field(default=None, index=True)
    api_hash: str | None = Field(default=None, index=True)
    access_expires_at: datetime | None = None
    refresh_hash: str | None = Field(default=None, index=True)
    prev_refresh_hash: str | None = Field(default=None, index=True)
    refresh_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    last_used: datetime | None = None
    revoked_at: datetime | None = None


# --- security / compliance ---
class Role:
    ADMIN = "admin"
    MANAGER = "manager"
    REVIEWER = "reviewer"
    LINGUIST = "linguist"  # specialist: AI/prompt tuning, TM/glossary, CAT + LQA
    TRANSLATOR = "translator"
    VIEWER = "viewer"
    ALL = ["admin", "manager", "reviewer", "linguist", "translator", "viewer"]  # noqa: RUF012
    # higher rank = more write privilege. linguist sits at reviewer level (can
    # write content); its *extra* power (AI/prompt setup) is a separate predicate
    # (security.can_tune_ai), not a rung on this ladder.
    RANK = {"viewer": 0, "translator": 1, "linguist": 2, "reviewer": 2, "manager": 3, "admin": 4}  # noqa: RUF012
    # human-friendly labels for the UI
    LABELS = {  # noqa: RUF012
        "admin": "Administrator",
        "manager": "Manager",
        "reviewer": "Reviewer",
        "linguist": "Linguist",
        "translator": "Translator",
        "viewer": "Viewer",
    }


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    name: str = ""
    role: str = Role.TRANSLATOR
    password_hash: str = ""
    auth_source: str = "local"  # local | sso | ldap | scim | saml | trust
    # SCIM 2.0 external id (Train I): the IdP's stable opaque id for this account
    # (Okta/Entra `externalId`). NULL for every non-SCIM account (byte-identical to
    # today). Indexed for the `externalId eq "..."` filter lookup. NOT unique at the
    # DB level here (email uniqueness — #74/Train Q3 — owns the UNIQUE migration; SCIM
    # must not add a competing constraint); the service adopts/updates by email.
    external_id: str | None = Field(default=None, index=True)
    # OIDC subject binding (identity-01): the issuer and `sub` of the OIDC identity this
    # account is linked to. Set on the first verified OIDC link (JIT creation or adoption
    # by a VERIFIED email); every later OIDC sign-in is matched by (issuer, sub), and an
    # email match whose binding names another subject is refused. NULL = not bound yet
    # (every pre-existing account after the upgrade binds on its next verified sign-in).
    sso_issuer: str | None = None
    sso_subject: str | None = Field(default=None, index=True)
    active: bool = True
    department: str = ""  # for content routing / department inbox (Phase B)
    ui_locale: str = ""  # preferred UI language ("" = follow the install default)
    ui_skin: str | None = None  # LEGACY, ignored since Console (2026-10-08): the old skin pick
    # Appearance (ADR 0063): "light" | "dark"; "" = Match my computer. Per user, rendered into
    # every signed-in page before first paint (deps.current_theme).
    ui_theme: str = Field(default="", sa_column_kwargs={"server_default": ""})
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # Customer binding (R2-3, item 3). NULL = INTERNAL STAFF (today's behaviour,
    # byte-identical): sees the whole install (subject to role + membership + org).
    # A non-NULL customer_id makes this a CUSTOMER-BOUND account: it may hold only
    # translator/linguist/viewer roles (never admin/manager — rejected at the
    # security seam) and its app-wide visibility is confined to that ONE customer's
    # projects (intersected with membership) and its /audit view to that customer's
    # rows. Set/cleared only by an admin (org-filtered picker); the SINGLE authority
    # for customer isolation. Inert (NULL) for every existing account, so a fresh /
    # upgraded install with no bound users is byte-identical to pre-R2-3.
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    # DORMANT as of 2026-09-20. This was the super-admin/operator flag, and its only
    # purpose was to open the /back-office door. That console and the cross-tenant account
    # it authenticated were both removed; org lifecycle is a host operator action now
    # (`cli org`, `cli user org`), keyed on LOC_TMS_OPERATOR_KEY rather than on any column.
    #
    # The FIELD is kept rather than dropped because a destructive migration is a worse risk
    # than a dead column, and this repo has been bitten by migrations beyond ADD COLUMN on
    # pre-rebuild installs. Nothing may read or write it: verify_multiorg fails if anything
    # under app/ does. Existing rows keep whatever value they had and it means nothing.
    # If a hosted offering ever needs an operator again, design it then; do not switch this
    # back on, because nothing around it still enforces the linchpin it relied on.
    is_superadmin: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    last_login: datetime | None = None
    # Session generation (#138). Every web session cookie carries the epoch it was
    # minted under; `security.session_user` refuses a cookie whose epoch does not
    # match this column, so bumping it (logout, password change) kills every
    # outstanding cookie for this user on every device at once. The session stays a
    # stateless signed cookie: no session table, no blacklist, one integer per user.
    # NOT NULL with server_default "0" so every pre-existing row reads 0 and an
    # upgraded install is byte-identical until someone logs out. Mirrored in the
    # alembic revision b4e1c7f29a06 + db._ADDED_COLUMNS["user"].
    session_epoch: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # Onboarding checklist (SPEC-onboarding-tour Phase 0). NULL = ELIGIBLE — the
    # first-run "Get started" setup checklist may auto-show once (admins/linguists
    # only, D3); a non-NULL timestamp = DISMISSED/complete and it never auto-shows
    # again (re-summon via ?tour=1 still overrides for display; it does NOT clear
    # the flag). Additive nullable. THE CRITICAL back-compat detail: the migration
    # BACKFILLS every pre-existing user to dismissed (COALESCE(last_login,
    # created_at, now)), so no live account ever sees the tour uninvited — only
    # accounts created AFTER the migration land NULL = eligible. Mirrored in the
    # alembic revision f9c2e7a4d1b8 + db._ADDED_COLUMNS["user"] SQLite shim (same
    # one-time-backfill-in-the-just-added-branch pattern as inbox_key /
    # webhookevent.processed_at). Chosen over a bool so we keep WHEN for analytics.
    onboarding_dismissed_at: datetime | None = None
    # Password policy (HECVAT AAAI-03). When the LOCAL password was last set; the
    # password_max_age_days check counts from it. NULL = never set here (an SSO/LDAP
    # account, or a pre-existing row the backfill found without a password) = never
    # expires. The migration backfills every row WITH a password to the upgrade time, so
    # turning max age on starts every existing account's clock then, never retroactively.
    # Mirrored in the alembic revision e5a1c8d3f7b2 + db._ADDED_COLUMNS["user"].
    password_changed_at: datetime | None = None
    # Set by an admin password reset: the person must choose a new password at the next
    # sign-in before anything else. Cleared by any self-chosen password change.
    password_must_change: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})


class PasswordHistory(SQLModel, table=True):
    """One PREVIOUS local password of one account, as its Argon2id hash (never plaintext),
    for the password_history reuse rule (HECVAT AAAI-03). A row is written every time a
    local password is set; only the newest `password_history` rows per user are kept.
    Never exported (GDPR export omits it like password_hash); deleted by GDPR erase.

    ON DELETE CASCADE (unlike the explicit child-before-parent deletes elsewhere): no
    product path hard-deletes a user, but an operator or a test fixture that does must not
    be blocked by, or leave behind, credential material that only exists for that user."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, ondelete="CASCADE")
    password_hash: str
    created_at: datetime = Field(default_factory=_utcnow)


class UserMfa(SQLModel, table=True):
    """One account's TOTP authenticator (RFC 6238; HECVAT AAAI-17 / DCTR-14). At most one
    row per user. `secret_enc` is the base32 shared secret as a Fernet ciphertext under
    config.ENCRYPTION_KEY (the key connector credentials use), never plaintext.

    `enabled_at` NULL = enrollment STARTED but not yet confirmed with a code (the secret is
    shown to the person, and nothing is enforced); non-NULL = two-step sign-in is ON.
    `last_step` is the newest 30-second time step a code was accepted for: a code for the
    same or an older step is refused (no replay). Written by app/services/mfa.py only.
    Never exported; deleted by GDPR erase and by an admin MFA reset. ON DELETE CASCADE for
    the same reason as PasswordHistory. Mirrored in the alembic revision c3d9a6f1e2b7."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True, ondelete="CASCADE")
    secret_enc: str
    enabled_at: datetime | None = None
    last_step: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_used_at: datetime | None = None
    # The PER-ACCOUNT wrong-code budget (gate round 1, MEDIUM-1): wrong codes from any
    # address inside the current window, when that window started, and until when every
    # code check for the account is locked. Persisted so a restart does not reset it.
    fail_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    fail_window_start: datetime | None = None
    locked_until: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class MfaRecoveryCode(SQLModel, table=True):
    """One single-use recovery code for two-step sign-in, as its Argon2id hash (like a
    password; the plaintext is shown once, when the set is generated). Ten per set; a new
    set replaces the old one. `used_at` non-NULL = spent. Never exported; deleted by GDPR
    erase, by disabling two-step sign-in and by an admin MFA reset. Mirrored in the alembic
    revision c3d9a6f1e2b7."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, ondelete="CASCADE")
    code_hash: str
    used_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class UserLayout(SQLModel, table=True):
    """One person's saved card layout for one page TYPE (SPEC-project-layout-editor
    section 9, train L1). A row exists only once the person saves; no row = the
    server default from app/services/page_layouts.py::REGISTRY, so a later change to a
    default never overwrites anyone's saved layout.

    `owner` is the identity key the unique constraint is on: "user:<id>" for a
    signed-in account, or "local" for the single shared layout of an install with
    sign-in OFF (no user exists there; user_id is then NULL). Keying uniqueness on this
    string, not on the nullable user_id, keeps one row per (owner, page) on SQLite and
    Postgres alike (NULLs never collide in a UNIQUE). The owner is always derived from
    the session / API token / CLI --user by the shell, never read from a request field.

    `layout_json` is the validated `{"v": 1, "rows": [[card, ...], ...]}` document,
    size-bounded by page_layouts.MAX_LAYOUT_BYTES. Not org-scoped: a layout is a UI
    preference of one account (the ApiKey/user_id precedent), never shared content."""

    __table_args__ = (UniqueConstraint("owner", "page_key", name="ux_userlayout_owner_page"),)
    id: int | None = Field(default=None, primary_key=True)
    owner: str = Field(index=True)
    user_id: int | None = Field(default=None, foreign_key="user.id", index=True)
    page_key: str
    layout_json: str = ""
    updated_at: datetime = Field(default_factory=_utcnow)


class UserGroup(SQLModel, table=True):
    """A SCIM 2.0 directory group (Train I). v1 groups are DIRECTORY METADATA ONLY:
    they carry the IdP's group name/externalId + membership so an admin can see how
    the directory is organised, but they have ZERO authorization effect — no role,
    no visibility, no project scoping is derived from group membership (that is
    post-demo policy work). Org-scoped exactly like every other tenant table: a SCIM
    key bound to org A only ever sees/creates org A groups. Name is unique per org
    (functional unique index ux_usergroup_org_name on (coalesce(org_id,-1), name), so
    gate-off NULL-org groups share one bucket — the ux_project_inbox_key precedent)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # SCIM externalId for the group (the IdP's stable id); NULL when unset.
    external_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class UserGroupMember(SQLModel, table=True):
    """One (group, user) membership edge (Train I). Directory metadata only — being a
    member confers NO authorization (see UserGroup). Unique per (group_id, user_id)
    (ux_usergroupmember_group_user) so a repeated SCIM `add` PATCH is idempotent."""

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="usergroup.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    added_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    actor: str = "system"  # user email, or "system"
    action: str = ""  # e.g. "login", "POST /projects", "settings.update"
    target: str = ""
    detail: str = ""
    ip: str = ""
    # Customer scope (R2-3, item 3). NULL = a SYSTEM / internal event (auth, admin,
    # settings, org, back-office) — invisible to customer-bound users. A non-NULL
    # customer_id ties the row to one customer (derived at the audit seam from the
    # event's project / connection / customer subject) so a customer-bound account's
    # /audit view shows ONLY its own customer's rows. Canonical v2 HMACs include
    # customer_id; bounded historical v1 rows excluded it. Existing rows are not
    # re-signed and retain their historical limitations. NULL means system.
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    # Org scope (#487): the organization the ACTOR belonged to when the row was WRITTEN
    # (stamped at the audit seam from the actor's account), so a user later moved to
    # another org does not carry the old org's history along. NULL = a system or
    # unauthenticated event, or a row from before this column. Like customer_id this is a
    # view/filter column OUTSIDE the HMAC-chained payload, so no existing row_hash changes.
    org_id: int | None = Field(default=None, index=True)
    # tamper-evidence: each row hashes its content + the previous row's hash
    prev_hash: str = ""
    row_hash: str = ""


class Invite(SQLModel, table=True):
    """A pending email invitation; consumed when the invitee sets a password."""

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    role: str = Role.TRANSLATOR
    token: str = Field(index=True)
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(default_factory=_utcnow)
    accepted: bool = False
    revoked_at: datetime | None = None
    org_id: int | None = None  # #129: durable ownership; NULL is operator-only under multi-org.


class LQAResult(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    document_id: int | None = Field(default=None, foreign_key="document.id")
    segment_id: int | None = Field(default=None, foreign_key="segment.id")
    dimension: str = ""  # MQM dimension, canonicalized at persist (lqa.canon_dimension)
    severity: str = ""  # canonical: critical | major | minor | neutral | other
    note: str = ""
    score: float | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    # --- Wave 2 attribution (SPEC-wave2-qa-pe-reporting §3.1) --------------------
    # Six ADDITIVE nullable columns, NO server default. NULL is the honest legacy value
    # ("written before Wave 2") and every report buckets NULL as `(untracked ...)`.
    # Stamped ONCE at INSERT on each finding row (§3.1.6): the row is the record of one
    # scoring event; a re-score deletes the prior rows and inserts fresh ones whose
    # attribution reflects state at re-score time.
    #
    # ONE WRITER UPDATES `dispatch_id` AFTER INSERT (2026-08-31), and it is the only one:
    # `retention.purge_vendor_dispatch_legs` SEVERS it (-> NULL) when the VendorDispatch leg
    # it points at is erased because that leg's WorkflowRun was deleted with its document.
    # The alternative was deleting the finding, which would destroy a STILL-LIVE document's
    # QA record as collateral of another document's erasure. Nothing else ever UPDATEs any
    # of these six columns.
    #   LAKEHOUSE RESIDUAL, disclosed honestly: `lqa_findings` is an APPEND_CURSOR stream
    #   (databricks_export.APPEND_CURSOR_STREAMS) keyed on row id, so a finding already below
    #   the high-water mark is NEVER re-emitted — it keeps its OLD dispatch_id in the
    #   lakehouse while the source row reads NULL, and there is no re-emit path to reconcile
    #   them. Same shape as the §6.7 withdrawn_at residual. (SegmentPostEdit's identical
    #   sever has NO such residual: `segment_post_edits` is a SNAPSHOT stream, so the next
    #   export carries the severed value.)
    target_locale: str | None = Field(default=None)  # the locale the card was scored for
    finding_source: str | None = Field(default=None)  # 'structural' | 'rulebook' | 'llm'
    judge: str | None = Field(default=None)  # 'provider/model' for llm, else NULL
    scoring_id: str | None = Field(default=None)  # uuid4 hex: THE card identity (§3.1.3)
    vendor_id: int | None = Field(default=None, foreign_key="vendor.id")
    dispatch_id: int | None = Field(default=None, foreign_key="vendordispatch.id")
    # --- Wave 3 reviewer-authored findings (SPEC-wave3-collaboration §6.1) --------
    # TWO additive nullable columns. Both NULL on every machine row, forever — a
    # machine finding has NO actor and is never withdrawn.
    #   actor        the logging reviewer's email. The table's FIRST PII column
    #                (Wave 2 §3.1.8 deferred it here explicitly). Swept AND exported
    #                by gdpr (§3.6/§6.1 — Art.17 + Art.15 symmetry).
    #   withdrawn_at NULL = live. Non-NULL = the author (or a manager) withdrew this
    #                finding (§6.7 soft delete). ONLY ever set on a finding_source=
    #                'human' row; a withdrawn row is excluded from every report bucket
    #                and the LQA page but RETAINED in the append-only lakehouse (L5).
    # `finding_source` (above) gains the value 'human' for these rows; `judge`,
    # `score` and `scoring_id` stay NULL (a hand-logged bug is not a scored card, §6.6).
    actor: str | None = Field(default=None)
    withdrawn_at: datetime | None = Field(default=None)


class SegmentPostEdit(SQLModel, table=True):
    """Episode-scoped post-edit capture (SPEC-wave2-qa-pe-reporting §3.2).

    ONE row per (segment, editing episode). An "editing episode" is a maximal run of
    consecutive edits by one actor identity (actor_kind, actor, vendor_id, dispatch_id)
    over an UNBROKEN TEXT LINEAGE — the episode closes when the actor changes OR when the
    text it last saw (`final_sha`) is not the text now present (a machine re-commit landed
    between two edits). Per-episode attribution is exact by construction; the cumulative
    QA-4 machine->final metric rides on the max-episode_seq row.

    A NEW TABLE, so create_all() makes it on the non-alembic SQLite path (the LLMemory
    precedent above — `_ADDED_COLUMNS` is for new COLUMNS on EXISTING tables only). A
    segment child: inherits every bulk-delete obligation LQAResult has (§3.2.6)."""

    __table_args__ = (
        # THE card identity of an editing episode; read-then-insert, so a concurrent
        # save on one segment can race it — the capture retries once (postedit.capture).
        UniqueConstraint("segment_id", "episode_seq", name="ux_segment_post_edit_seq"),
    )
    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(foreign_key="segment.id", index=True)  # NOT unique
    # Denormalized: avoids the per-row session.get N+1 that lqa_findings records as a
    # deferred cost (databricks_export.py:484-486), and gives the report/lakehouse a
    # dict-hit doc->project map (§5.3 "No N+1").
    document_id: int = Field(foreign_key="document.id", index=True)
    target_locale: str = ""
    episode_seq: int = 0  # 1-based; UNIQUE with segment_id
    unit_kind: str = "segment"  # 'segment' | 'full_text' (§3.2.4 seam 3)
    # N1 lineage marker: sha256 hex of the text this episode LAST wrote. The episode
    # closes when sha(pre) != final_sha, so no episode's baseline ever describes text
    # that was subsequently replaced by someone else.
    final_sha: str = ""
    # --- attribution (immutable for the life of the episode row, with ONE exception:
    #     `dispatch_id` is SEVERED to NULL by retention.purge_vendor_dispatch_legs when the
    #     VendorDispatch leg it references is erased along with its WorkflowRun/document —
    #     2026-08-31. Deleting the episode instead would destroy a still-live document's
    #     post-edit record as collateral of ANOTHER document's erasure (document_id/segment_id
    #     here are NOT NULL, so such a row provably belongs to a live document). No lakehouse
    #     residual: `segment_post_edits` is a SNAPSHOT stream, so the next export carries the
    #     severed value — unlike LQAResult.dispatch_id, whose stream is append-cursor.) ---
    actor: str = ""  # editing user's email ('' when unattributed/vendor)
    actor_kind: str = ""  # 'human' | 'vendor'
    vendor_id: int | None = Field(default=None, foreign_key="vendor.id")
    dispatch_id: int | None = Field(default=None, foreign_key="vendordispatch.id")
    # --- this episode's baseline (QA-4's "raw_mt retention") ---
    baseline_text: str = ""  # the target text at the START of this episode
    baseline_kind: str = ""  # 'mt' | 'tm' | 'human' | 'none'
    baseline_chars: int = 0
    final_chars: int = 0
    source_words: int = 0  # M4: _derived_words(segment.source_text) at capture
    # --- metrics ---
    distance_char: int | None = None  # episode-local: baseline -> current
    distance_word: int | None = None
    distance_pct: float | None = None
    cum_distance_char: int | None = None  # origin baseline -> current
    cum_distance_word: int | None = None
    cum_distance_pct: float | None = None
    metric_version: str = "pe/1"
    edit_count: int = 0
    first_edited_at: datetime = Field(default_factory=_utcnow)
    last_edited_at: datetime = Field(default_factory=_utcnow, index=True)  # date-range column


# --- Queries / comments (SPEC-wave3-collaboration §3) ------------------------- #
# Threaded query/comment entity: QueryThread carries identity, scope, lifecycle and
# assignment ONCE; QueryPost is its append-only message log (§3.1). Both are NEW
# TABLES, so a non-alembic SQLite boot gets them from create_all() (app/db.py
# `_ADDED_COLUMNS` is for new COLUMNS on EXISTING tables only — the SegmentPostEdit /
# LLMemory precedent above). QueryThread is a project/document/segment/key child and
# QueryPost is its child; both are deleted child-before-parent at every §3.5 site
# (never via ondelete=CASCADE), so Postgres raises no FK error.
class QueryStatus:
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"
    ALL = (OPEN, ANSWERED, CLOSED)


class QueryThread(SQLModel, table=True):
    __table_args__ = (
        # Wire idempotency for the thread-CREATING vendor branch (§7.3/MED-4): a
        # re-admitted vendor comment with no reply_to has no thread yet, so the
        # POST-level constraint below cannot dedup it — each attempt would mint a new
        # thread. This thread-level uniqueness is the backstop. SQLite and Postgres
        # both treat NULLs as DISTINCT, so every HUMAN thread (dispatch_id AND
        # external_id both NULL) is unconstrained; only a vendor thread's
        # (dispatch_id, external_id) pair is unique.
        UniqueConstraint("dispatch_id", "external_id", name="ux_querythread_vendor_external"),
    )
    id: int | None = Field(default=None, primary_key=True)
    # THE TENANT ANCHOR. NOT NULL, always resolved at creation from whatever the thread
    # was scoped to (segment -> document -> project; key -> project). It is the only
    # column access.visible_project_ids speaks (§2.2), so every list/read/write
    # predicate filters on it and nothing else. Named as the HIGH-severity surface of
    # the wave.
    project_id: int = Field(foreign_key="project.id", index=True)
    document_id: int | None = Field(default=None, foreign_key="document.id", index=True)
    segment_id: int | None = Field(default=None, foreign_key="segment.id", index=True)
    # ADVISORY on a key-scoped thread (§3.1/MED-8): keys._move_value_rows re-parks a
    # key's value segments under a different internal document on a namespace rename
    # WITHOUT touching the project, so document_id can drift stale. The tenant anchor
    # (project_id) is unaffected — a key's values never move to another project — and
    # key_id-scoped reads resolve their document at READ time from StringKey.
    key_id: int | None = Field(default=None, foreign_key="stringkey.id", index=True)
    target_locale: str = ""  # "" = not locale-specific (project/document-wide)
    subject: str = ""  # flattened + length-refused at the seam (§3.4)
    status: str = QueryStatus.OPEN
    author: str = ""  # opener's email; "" for a vendor-originated thread
    author_kind: str = "human"  # 'human' | 'vendor'
    assignee_email: str = ""  # "" = unassigned = nobody is emailed (the WorkflowStep rule)
    vendor_id: int | None = Field(default=None, foreign_key="vendor.id")
    dispatch_id: int | None = Field(default=None, foreign_key="vendordispatch.id")
    # The vendor's own comment ref on a vendor-ORIGINATED thread (reply_to null branch,
    # §7.3/MED-4); NULL for every human thread. Half of the vendor-external unique key.
    external_id: str | None = Field(default=None)
    post_count: int = 0  # denormalized display/ordering value; NEVER a
    # predicate/cap/report input (L4). Maintained only
    # inside query_threads.add_post / record_vendor_post.
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)  # list sort + date window
    closed_at: datetime | None = None
    closed_by: str = ""


class QueryPost(SQLModel, table=True):
    __table_args__ = (
        # Wire idempotency: a re-admitted vendor return must never duplicate posts.
        # SQLite and Postgres both treat NULLs as DISTINCT, so the many HUMAN posts
        # (external_id NULL) are unconstrained while a vendor post's (thread,
        # vendor-supplied id) is unique (§3.1).
        UniqueConstraint("thread_id", "external_id", name="ux_querypost_external"),
    )
    id: int | None = Field(default=None, primary_key=True)
    thread_id: int = Field(foreign_key="querythread.id", index=True)
    body: str = ""
    author: str = ""  # email; "" for a vendor post (no vendor user accounts)
    author_kind: str = "human"  # 'human' | 'vendor'
    author_label: str = ""  # vendor-side display name/role, verbatim, never a User
    vendor_id: int | None = Field(default=None, foreign_key="vendor.id")
    dispatch_id: int | None = Field(default=None, foreign_key="vendordispatch.id")
    external_id: str | None = Field(default=None)  # vendor's own comment ref; NULL for human
    created_at: datetime = Field(default_factory=_utcnow, index=True)


# --- LLMemory (PROPOSAL-llmemory Phase 1) ------------------------------------ #
# A first-class module beside TM and Glossary: typed, scoped, provenance-linked
# MEMORIES distilled from workflow events (reviewer corrections, adjudicated LQA
# findings, manual notes) and recalled — tiny-k, relevance-gated — into translate
# prompts. Three tables: MemoryEntry (the belief), MemoryEvent (the lossless
# episode/provenance layer), MemoryEvidence (the join proving which events justified
# which belief). All three are NEW TABLES, so a non-alembic SQLite boot gets them from
# create_all() (app/db.py `_ADDED_COLUMNS` is for new COLUMNS on EXISTING tables only —
# same posture as workflowgroup / contexttype / styleguide).
class MemoryKind:
    """What a MemoryEntry asserts. Plain strings (SQLite-friendly), like every other
    "enum" in this file.

    TERM_DECISION      — a source phrase consistently retranslated to a specific target
                         phrase by reviewers. The only kind with a DETERMINISTIC CONFLICT
                         KEY (scope + normalized source phrase) and the only kind recall
                         matches lexically against a segment's source.
    CORRECTION_PATTERN — a recurring non-term reviewer edit (punctuation/format class).
    TONE_RULE          — register/voice guidance distilled from surviving LQA findings.
    STYLE_CONSTRAINT   — mechanical style guidance distilled from surviving LQA findings.
    AUDIENCE_NOTE      — who the content is for (Theme-3 prep; written by manual notes).
    PROJECT_NOTE       — free-form project guidance a human wrote down.

    LLMemory NEVER outranks the glossary/DNT/style guide — it is additive context that
    ranks BELOW them (proposal §2, §5 precedence law)."""

    TERM_DECISION = "term_decision"
    CORRECTION_PATTERN = "correction_pattern"
    TONE_RULE = "tone_rule"
    STYLE_CONSTRAINT = "style_constraint"
    AUDIENCE_NOTE = "audience_note"
    PROJECT_NOTE = "project_note"
    ALL = (
        TERM_DECISION,
        CORRECTION_PATTERN,
        TONE_RULE,
        STYLE_CONSTRAINT,
        AUDIENCE_NOTE,
        PROJECT_NOTE,
    )
    # The kinds recall matches LEXICALLY against a segment's source text.
    TERM_KINDS = (TERM_DECISION,)
    # The kinds eligible for the single doc-level note slot (proposal §5: "at most 1
    # tone/style/note entry per call").
    NOTE_KINDS = (TONE_RULE, STYLE_CONSTRAINT, AUDIENCE_NOTE, PROJECT_NOTE, CORRECTION_PATTERN)


class MemoryStatus:
    """A MemoryEntry's lifecycle state. **ACTIVE is the whole of "current state"** —
    every recall query is `status == ACTIVE` (and, defensively, `superseded_at IS NULL`,
    which supersession sets in the same transaction that flips status).

    The proposal (§3) names three states — active/invalidated/purged. Two more ship here
    because §4 requires them to be QUERYABLE and they have nowhere else to live:

    CANDIDATE  — distillation saw evidence but NOT enough to admit it (the "one event =
                 candidate, invisible to recall" rule). Stored rather than discarded so
                 the module page can show a candidates panel and so a second event
                 PROMOTES the same row instead of racing to create a duplicate.
    CONFLICTED — two different values for ONE deterministic key are BOTH accumulating in
                 the recent window. Disagreement SUPPRESSES the write (adaptive-MT lesson
                 #2): neither value activates, and both rows are parked here so the
                 module page can list the disagreement. Entries sharing a conflicted key
                 are found by re-deriving the key (scope + source_term_norm) — no extra
                 column needed, since a conflict is by definition a key collision.

    ACTIVE      — current, recall-eligible.
    INVALIDATED — no longer current. `superseded_at`/`superseded_by_id` non-NULL means it
                  was SUPERSEDED by a newer belief (the bi-temporal chain); both NULL
                  means a human/rollback invalidated it. Never deleted, never edited in
                  place.
    PURGED      — erasure obligation honoured: content columns nulled/blanked, the row
                  kept as a TOMBSTONE (id + scope + timestamps) so the audit chain and
                  the supersession chain never develop a hole."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    INVALIDATED = "invalidated"
    PURGED = "purged"
    ALL = (CANDIDATE, ACTIVE, CONFLICTED, INVALIDATED, PURGED)


class MemoryOrigin:
    """How a MemoryEntry came to exist — its trust tier.

    MANUAL          — a human wrote it. The HIGHEST tier and the ONLY origin that
                      activates on first evidence (proposal §4 item 4).
    REVIEWER_EDIT   — distilled from >=2 independent reviewer corrections made by >=2 distinct
                      human actors (#358).
    LQA_ADJUDICATED — distilled from >=3 surviving (non-neutralized) LQA findings."""

    MANUAL = "manual"
    REVIEWER_EDIT = "reviewer_edit"
    LQA_ADJUDICATED = "lqa_adjudicated"
    ALL = (MANUAL, REVIEWER_EDIT, LQA_ADJUDICATED)


class MemoryEventKind:
    """The episode types the capture API records. Events are LOSSLESS and cheap: capture
    is synchronous and event-only, distillation is a separate batch pass (proposal §4)."""

    SEGMENT_EDIT = "segment_edit"
    RESUME_EDIT = "resume_edit"
    LQA_FINDING = "lqa_finding"
    MANUAL = "manual"
    ALL = (SEGMENT_EDIT, RESUME_EDIT, LQA_FINDING, MANUAL)


class MemoryEntry(SQLModel, table=True):
    """One typed, scoped, provenance-linked MEMORY (proposal §3).

    SCOPE IS A HARD PARTITION. `customer_id` is NOT NULL by design: there is no such
    thing as a customer-less memory, and "no cross-customer memory ever" (§2) is
    enforced by every read filtering on it. `project_id`/`source_locale`/`target_locale`
    are nullable = BROADER (customer-wide / pair-agnostic); resolution is
    most-specific-wins, mirroring style-guide precedence (style_guides.resolve_style_guide).

    BI-TEMPORAL, NEVER OVERWRITTEN. A changed belief is a NEW row plus
    `superseded_at`/`superseded_by_id` stamped on the old one — so "what did we believe
    on date X" is a chain walk and "what do we believe now" is `status == ACTIVE`.

    `audience`/`intent` ship from day one per the maintainer's locked call, but NOTHING reads
    them until audience profiles exist (Theme 3) — they are written by manual notes and
    carried through, never used as a recall filter in Phase 1.

    SCOPE INDEX (non-unique, deliberately — mirrors ix_styleguide_scope): several
    entries legitimately share a scope (a candidate plus the active entry it will
    promote to, a superseded chain, two conflicted values). A functional index over
    COALESCE-collapsed scope columns speeds the recall reads on both SQLite expression
    indexes and Postgres without blocking any of that."""

    __table_args__ = (
        Index(
            "ix_memoryentry_scope",
            text("coalesce(org_id, -1)"),
            text("customer_id"),
            text("coalesce(project_id, -1)"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    kind: str = MemoryKind.PROJECT_NOTE
    status: str = MemoryStatus.CANDIDATE
    # --- scope (customer_id REQUIRED = the hard partition) ---
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int = Field(foreign_key="customer.id", index=True)
    project_id: int | None = Field(default=None, foreign_key="project.id")
    # Stored canonical (es_ES -> es-ES), compared normalized — GlossaryPair's convention.
    # NULL = pair-agnostic (applies to every pair in scope).
    source_locale: str | None = Field(default=None)
    target_locale: str | None = Field(default=None)
    # --- content ---
    source_term: str = ""  # term kinds only
    target_term: str = ""  # term kinds only
    # The DETERMINISTIC CONFLICT KEY's variable part: source_term lowercased with
    # whitespace collapsed. Indexed because it is the distiller's and the recall
    # matcher's lookup column. "" for non-term kinds.
    source_term_norm: str = Field(default="", index=True)
    body: str = ""  # rules/notes (capped at admission, never a prompt fence)
    audience: str | None = Field(default=None)  # Theme-3 prep; unread by recall
    intent: str | None = Field(default=None)  # Theme-3 prep; unread by recall
    # --- trust/quality ---
    origin: str = MemoryOrigin.MANUAL
    confidence: float = 0.0  # evidence-count-derived (see llmemory._confidence)
    evidence_count: int = 0
    # --- bi-temporal audit core ---
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: str = ""  # actor email / "system"
    superseded_at: datetime | None = Field(default=None)
    superseded_by_id: int | None = Field(default=None, foreign_key="memoryentry.id")
    invalidated_at: datetime | None = Field(default=None)
    purged_at: datetime | None = Field(default=None)
    # --- usage (decay/janitor input, Phase 2) ---
    last_used_at: datetime | None = Field(default=None)
    use_count: int = 0
    # --- lifecycle: janitor + human adjudication (Phase 2, SPEC-llmemory-phase2 §5) ---
    # SUPPRESSION MARKER (§5.2, F4/F2): stamped when the janitor decays/expires a belief.
    # `_upsert_term`'s re-emergence branch refuses to re-mint a suppressed belief unless
    # genuinely NEW evidence (created AFTER this window) arrives; it is ALSO the "aged out"
    # discriminator that tells a decayed entry apart from a human `invalidate` (both leave
    # superseded_at NULL). NULL on every pre-Phase-2 / non-decayed row = byte-identical.
    suppressed_until: datetime | None = Field(default=None)
    # HUMAN-ADJUDICATION MARKER (§5.5, F5): stamped on the WINNER by `resolve_conflict`.
    # `_distill_terms`' disagreement branch short-circuits for a key with a live
    # adjudication (no newer post-adjudication evidence), so distill can never flip a
    # human's chosen winner back to CONFLICTED or mint CONFLICTED duplicates of the losers.
    adjudicated_at: datetime | None = Field(default=None)
    adjudicated_by: str = ""  # actor email of the human who resolved the conflict


class MemoryEvent(SQLModel, table=True):
    """One captured EPISODE — the lossless provenance layer under MemoryEntry.

    Capture is cheap and never distills inline: a reviewer save, a resume-with-edits
    diff, or a surviving LQA finding writes exactly one row here and returns. The batch
    distiller reads these and decides what (if anything) is admissible.

    `customer_id` is NULLABLE here (unlike MemoryEntry): a document can live in a
    project with no customer, and an event on such a document is still worth recording
    honestly. It simply can NEVER distill into an entry (the distiller skips NULL-
    customer events) — the safe direction.

    SOURCE-DOCUMENT TEXT IS NEVER AN INSTRUCTION SOURCE. Events come only from human
    actions and adjudicated findings, so `before_text`/`after_text` are human-authored
    or human-approved strings, not raw uploaded content (MINJA surface minimized,
    proposal §4)."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str = MemoryEventKind.MANUAL
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    project_id: int | None = Field(default=None, foreign_key="project.id")
    document_id: int | None = Field(default=None, foreign_key="document.id")
    segment_id: int | None = Field(default=None, foreign_key="segment.id")
    workflow_run_id: int | None = Field(default=None, foreign_key="workflowrun.id")
    source_locale: str = ""
    target_locale: str = ""
    # Edit kinds: the target text BEFORE and AFTER the human touched it.
    before_text: str = ""
    after_text: str = ""
    # LQA kind: the surviving finding's MQM dimension / severity / offending span / note.
    dimension: str = ""
    severity: str = ""
    span: str = ""
    note: str = ""
    actor: str = ""
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class MemoryEvidence(SQLModel, table=True):
    """The join proving WHICH events justified a memory (the Copilot/Zep citation
    pattern). Every entry the distiller admits cites the exact events behind it, so the
    module page can show a belief's receipts and `purge` can cascade entry -> evidence ->
    events that no surviving memory still cites."""

    id: int | None = Field(default=None, primary_key=True)
    memory_id: int = Field(foreign_key="memoryentry.id", index=True)
    event_id: int = Field(foreign_key="memoryevent.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow)


# --- LLMemory workflow-suggestion queue (PROPOSAL-llmemory §10 Phase 3; Train A) ---
# NEW TABLES (memorysuggestion + memorysuggestionevidence), so a non-alembic SQLite boot
# gets them from create_all() with no app/db.py `_ADDED_COLUMNS` entry (that shim is for
# new COLUMNS on EXISTING tables only) -- the same posture as the three Phase-1 tables above.
class MemorySuggestionKind:
    """What a MemorySuggestion proposes to change about a workflow. Plain strings,
    like every other "enum" in this file. Each maps DETERMINISTICALLY onto explicit
    `workflows.*` seam calls at apply time (llmemory_suggestions.apply, §4.5).

    ADD_FIX_STEP     — R1: recurring failures of an EXISTING LQA step ⇒ insert a fix-loop
                       llm_agent step gated `prev_lqa:failed` after the LQA step.
    STEP_MODEL_PIN   — R2: repeated resume-with-edits against a step's output ⇒ pin the
                       workflow's configured fallback_model on that step (or flag it).
    ENABLE_RECALL    — R3: accumulated ACTIVE LQA-derived rules a workflow ignores ⇒
                       clear `memory_mode` on the eligible translate steps.

    The rules deliberately propose ADDITIVE / opt-in changes only -- never a delete, and
    never a routing change that removes a human_review / approval step (§4.3)."""

    ADD_FIX_STEP = "add_fix_step"
    STEP_MODEL_PIN = "step_model_pin"
    ENABLE_RECALL = "enable_recall"
    ALL = (ADD_FIX_STEP, STEP_MODEL_PIN, ENABLE_RECALL)


class MemorySuggestionStatus:
    """A MemorySuggestion's lifecycle state (llmemory_suggestions §4.2).

    PROPOSED   — machine-drafted, un-acted. The ONLY state generation produces.
    APPROVED   — a named reviewer approved it; apply is still a SEPARATE explicit act.
    APPLYING   — the in-flight guard state (F4): a CAS moved the row here BEFORE the
                 workflow mutation, and APPLIED is stamped only AFTER it lands -- so a
                 crash mid-apply leaves APPLYING, never a false APPLIED (LAW 1). A stuck
                 APPLYING row is a reconciliation signal, not a success.
    APPLIED    — the workflow-config mutation actually landed. Terminal.
    REJECTED   — a reviewer rejected it. Terminal, tombstoned (kept for audit; never deleted).
    SUPERSEDED — a fresher regeneration of the same target supersedes this stale open
                 proposal (bi-temporal, like MemoryEntry). Terminal.
    STALE      — apply found the target workflow had MOVED since draft (base_fingerprint
                 mismatch). Terminal; regenerate to get a fresh proposal.
    BLOCKED    — apply refused: either a CLEAN pre-seam refusal (a live run on the target;
                 RETRYABLE via retry_blocked -> APPROVED) or a PARTIAL two-commit patch that
                 failed after add_step (NOT retryable -- manual reconciliation from
                 applied_snapshot, since add_step has no idempotency key). The audit detail
                 flags clean-vs-partial (§4.5, §4.9)."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    STALE = "stale"
    BLOCKED = "blocked"
    ALL = (PROPOSED, APPROVED, APPLYING, APPLIED, REJECTED, SUPERSEDED, STALE, BLOCKED)
    #: Statuses an open (un-acted, still-actionable) proposal can be in -- the set
    #: generation reconciles against and supersedes.
    OPEN = (PROPOSED, APPROVED)


class MemorySuggestion(SQLModel, table=True):
    """One deterministically-drafted proposal to change a workflow's routing / step-order /
    config, each citing the exact MemoryEvents/entries/LQAResults that justify it plus a
    human-readable diff (PROPOSAL-llmemory §10 Phase 3; SPEC-llmemory-phase2 §4).

    APPLY IS AN EXPLICIT HUMAN ACT, NEVER AUTOMATIC (the maintainer's locked call #1, LAW 1).
    Generation drafts PROPOSED rows only and never touches config; a named reviewer
    approves/rejects; a named admin applies. There is no timer/scheduler/MCP/api-without-a-
    human path that mutates a workflow from a suggestion.

    SCOPE IS A HARD PARTITION, exactly like MemoryEntry: `customer_id` is NOT NULL and every
    read filters on it plus the fail-closed org predicate (mirror llmemory._active_entries).
    No cross-customer suggestion, citation, or apply-target is ever reachable (§4.10).

    BI-TEMPORAL, NEVER HARD-DELETED (LAW 2): a rejected suggestion is a terminal TOMBSTONE
    kept for audit; a superseded one carries `superseded_at`/`superseded_by_id` like an
    entry; a purge of cited evidence blanks the content columns but keeps the row as a
    scope/timestamp tombstone (§4.11).

    SCOPE INDEX (non-unique, mirrors ix_memoryentry_scope): several suggestions legitimately
    share a scope (open + superseded, several kinds for one workflow), so a functional index
    over COALESCE-collapsed scope columns speeds the customer-scoped reads without a unique
    constraint that would block the design."""

    __table_args__ = (
        Index(
            "ix_memorysuggestion_scope",
            text("coalesce(org_id, -1)"),
            text("customer_id"),
            text("coalesce(project_id, -1)"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    kind: str = ""  # one of MemorySuggestionKind.ALL
    status: str = MemorySuggestionStatus.PROPOSED
    # --- scope (customer_id REQUIRED = the hard partition, like MemoryEntry) ---
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int = Field(foreign_key="customer.id", index=True)
    project_id: int | None = Field(default=None, foreign_key="project.id")
    # --- apply target ---
    # workflow_id NOT NULL: a NULL/GLOBAL-scope workflow is refused at generation unless its
    # runs resolve to exactly ONE customer (§4.3). target_step_id is BY ID, never `key`
    # (WorkflowStep.key is nullable/NULL by default, so keying on it collapses the grouping).
    workflow_id: int = Field(foreign_key="workflow.id", index=True)
    target_step_id: int | None = Field(default=None, foreign_key="workflowstep.id")
    # A deterministic hash over the target workflow's ordered steps (the fields the patch
    # touches + the count and ids of steps), STAMPED AT DRAFT. The schema records no workflow
    # mtime/version, so this column IS the "did the workflow move" signal: apply recomputes
    # and compares, refusing a stale diff (§4.5, finding #2).
    base_fingerprint: str = ""
    # Deterministic dedup key hash(kind, workflow_id, target_step_id, rule-scope): generation
    # reconciles against open proposals by it rather than duplicating (§4.4).
    suggestion_key: str = Field(default="", index=True)
    rationale: str = ""  # human-readable, capped BODY_CAP, screened by refusal perimeter
    # The structured, deterministically-appliable patch (§4.5 maps each kind onto explicit
    # workflows.* seam calls).
    proposed_change: dict | None = Field(default=None, sa_column=Column(JSON))
    # The rendered human-readable diff, INCLUDING propagation outcome; angle-brackets
    # neutralized like _render_line so a suggestion can never forge a prompt fence.
    diff_text: str = ""
    evidence_count: int = 0
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    created_by: str = "system"  # "system" for generated
    reviewed_at: datetime | None = Field(default=None)
    reviewed_by: str = ""  # approve/reject actor email
    applied_at: datetime | None = Field(default=None)
    applied_by: str = ""  # apply actor email
    # Pre-change config snapshot of every step the patch touched, captured immediately
    # before the first seam call (undo-by-hand; the ONLY reconstruction path after a
    # partial-patch failure -- §4.5). {"steps": [...]} so the JSON column stays a dict.
    applied_snapshot: dict | None = Field(default=None, sa_column=Column(JSON))
    superseded_at: datetime | None = Field(default=None)
    superseded_by_id: int | None = Field(default=None, foreign_key="memorysuggestion.id")
    reject_reason: str = ""  # audited reason, capped
    # The BLOCKED-state discriminator, a dedicated status flag (NOT content) so GDPR purge —
    # which blanks the content columns — never erases the retry gate. "" for a non-blocked
    # row; "clean" = a pre-seam refusal (a live run), the ONLY retryable block; "partial" = a
    # two-commit patch that failed after add_step / a recovered stuck applying / a tombstoned
    # or ineligible target — never auto-retryable (manual reconciliation from applied_snapshot).
    blocked_kind: str = ""


class MemorySuggestionEvidence(SQLModel, table=True):
    """The join proving WHICH rows justified a suggestion (the MemoryEvidence pattern, for
    suggestions). A suggestion cites LQAResult rows (R1), MemoryEvents (R2), or MemoryEntries
    (R3); all three ref FKs are nullable and EXACTLY ONE is set per row (enforced at
    construction in the service, not by a DB constraint). purge_suggestions_for deletes the
    rows pointing at an entry/event being erased (§4.11)."""

    id: int | None = Field(default=None, primary_key=True)
    suggestion_id: int = Field(foreign_key="memorysuggestion.id", index=True)
    event_id: int | None = Field(default=None, foreign_key="memoryevent.id")
    memory_id: int | None = Field(default=None, foreign_key="memoryentry.id")
    lqa_result_id: int | None = Field(default=None, foreign_key="lqaresult.id")
    created_at: datetime = Field(default_factory=_utcnow)


# --- background jobs ---
class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(SQLModel, table=True):
    """A long-running unit of work executed off the request thread (document
    translation, connector runs). Routes enqueue and redirect immediately; the
    UI polls /jobs/{id}/status. Survives inspection after restarts — interrupted
    jobs are marked failed on startup by jobs.recover_interrupted()."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "translate_document" | "connector_translate"
    status: str = Field(default=JobStatus.QUEUED, index=True)
    payload: dict | None = Field(default=None, sa_column=Column(JSON))
    result: dict | None = Field(default=None, sa_column=Column(JSON))
    progress_done: int = 0
    progress_total: int = 0
    message: str = ""
    error: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # #364: the EXACT USD the spend ledger booked while this job ran (every run_completion /
    # complete_direct / record_usage call it made, failover and failed attempts included, a
    # free engine's calls 0). Written ONLY by the job runner / live-stream settle / sync
    # recorder from the ledger's own figure; no API, CLI or web door accepts it. NULL =
    # unknown (a row from before this column, a cut-off live stream): reports and project
    # budgets then re-price the result tokens as before (reports._job_cost).
    cost_usd: float | None = Field(default=None)


class JobQueueState:
    """The queue-entry lifecycle for the DURABLE (DB-backed) job queue -- the
    pending/running/done/failed state machine of a ``JobQueue`` row. Distinct from
    ``JobStatus`` on purpose: ``JobStatus`` is the EXECUTION status the handlers /
    reports / UI read off the ``Job`` row (unchanged), while these four values are the
    DISPATCH state the ``job_queue_backend="db"`` claim/complete machinery owns. On the
    default ``"memory"`` backend NOTHING writes this table, so the values are inert."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobQueue(SQLModel, table=True):
    """The DURABLE job-queue ledger backing ``job_queue_backend="db"`` (sprint Train S).

    One row per dispatched ``Job``. It is the seam a DB-backed / HA queue needs and the
    ``Job`` table cannot be: a worker CLAIMS a row with an atomic
    ``UPDATE...WHERE state='pending'`` compare-and-swap (so two workers -- even on two
    nodes -- can never double-claim), stamps a ``worker`` + ``lease_expires_at`` lease,
    and on restart a ``running`` row whose lease EXPIRED returns to ``pending`` (its work
    is re-dispatched, not silently failed). ``kind`` / ``payload`` mirror the ``Job`` at
    enqueue time for the HA seam (a remote worker can read the row without the Job), but
    the AUTHORITATIVE execution still runs off ``jobs._execute(job_id)`` reading the
    ``Job`` row -- this table is the DISPATCH ledger, never a second execution record.

    DORMANT on the default ``memory`` backend: the ``InMemoryJobQueue`` adapter never
    touches this table (it wraps the same ``queue.Queue`` as before), so a default install
    is byte-identical to pre-Train-S -- the table is created (create_all / the migration)
    but stays empty. NO foreign key on ``job_id`` deliberately: a queue entry can outlive
    a purged/erased Job (retention, GDPR) without an FK cascade dragging the ledger, and
    the dispatch path only ever reads ``job_id`` to hand to ``_execute``."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)  # the Job this entry dispatches (no FK -- see docstring)
    kind: str  # mirrors Job.kind at enqueue (HA seam)
    payload: dict | None = Field(default=None, sa_column=Column(JSON))  # mirrors Job.payload
    state: str = Field(default=JobQueueState.PENDING, index=True)  # pending|running|done|failed
    attempts: int = 0  # claim count (incremented on each atomic claim; the HA retry counter)
    worker: str | None = None  # the lease holder's worker id (NULL when unclaimed / released)
    lease_expires_at: datetime | None = None  # lease deadline; a running row past it re-queues
    enqueued_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow)


# --- integrations / connectors ---
class ConnStatus:
    NEW = "new"
    CONNECTED = "connected"
    ERROR = "error"
    REAUTH = "reauth"  # OAuth token refresh failed — user must reconnect


class Connection(SQLModel, table=True):
    """A configured integration with an external system (Jira, Airtable, …).

    `config` holds non-secret settings (base id, project key, field mappings);
    `secret_enc` is a Fernet-encrypted JSON blob of credentials (API tokens).
    Pulling content creates/uses `project_id`; pushing writes translations back."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "airtable" | "jira" | "confluence"
    name: str
    config: dict | None = Field(default=None, sa_column=Column(JSON))
    secret_enc: str = ""  # encrypted credentials (never plaintext)
    status: str = ConnStatus.NEW
    last_message: str = ""  # last test/sync result, for the UI
    project_id: int | None = Field(default=None, foreign_key="project.id")
    # Customer scope (P4, item 5). NULL = global/unassigned (byte-identical to the
    # pre-P4 behaviour — every existing connection stays NULL and keeps working). A
    # customer-scoped connection is only offered/listed inside THAT customer's
    # contexts; the connectors index shows global + the caller-selected customer's,
    # never another customer's (data isolation between customers).
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    # Content type default (SPEC-content-types phase 3, §6): copied onto the auto-created
    # sync project in store._ensure_project so a connection's imported content lands in
    # the right domain. NULL = unset (the auto-created project stays untyped/General —
    # byte-identical to today). Settable at connection create/edit on all 3 surfaces.
    content_type_id: int | None = Field(default=None, foreign_key="contenttype.id")
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    last_test: datetime | None = None
    last_sync: datetime | None = None


class SyncLog(SQLModel, table=True):
    """One pull/push run against a connection — for the connection's history view."""

    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="connection.id", index=True)
    direction: str = "pull"  # pull | push | test
    ok: bool = True
    count: int = 0  # items pulled/pushed
    message: str = ""
    actor: str = ""
    ts: datetime = Field(default_factory=_utcnow)


class WebhookEvent(SQLModel, table=True):
    """Idempotency ledger for the generalized connector webhook intake
    (CONNECTOR_FRAMEWORK_DESIGN §3.4). One row per (connection, event id) the
    intake has ALREADY accepted, so a provider RETRY (Zendesk/Intercom retry a
    slow/failed delivery) is deduped: the second delivery of the same event id
    ACKs 200 but enqueues nothing.

    The uniqueness is (connection_id, event_id) — scoped to the connection so a
    forged event id from one connection can never suppress another's, and so the
    connection_id lookup (which resolves the org) keeps intake from leaking across
    orgs. `event_id` is the provider's own id/idempotency key; when a provider
    sends none we derive a stable one from a hash of the payload (see
    webhooks.dedup_key)."""

    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="connection.id", index=True)
    event_id: str = Field(index=True)  # provider event id / derived hash
    kind: str = ""  # connector kind, for diagnostics
    job_id: int | None = None  # the enqueued job, when one was
    received_at: datetime = Field(default_factory=_utcnow)
    # D4 (audit) SETTLED marker: set once the intake has fully accepted-and-recorded
    # the event, WHETHER OR NOT a job was enqueued. The default Jira button-trigger
    # posture records PENDING and enqueues NO job, so job_id stays NULL forever;
    # keying dedup on job_id therefore let every replay of one captured, validly-
    # signed POST re-accept as fresh (unbounded replay). Dedup keys on THIS instead:
    # a replay of a SETTLED delivery is a duplicate; an event recorded but never
    # settled (a crash between record_event and settle_event) keeps processed_at NULL
    # and stays retryable, preserving the crash-window recovery. Nullable; the
    # one-time upgrade backfill sets it to received_at for pre-existing rows.
    processed_at: datetime | None = None


class ConnectorIdMap(SQLModel, table=True):
    """The source->target document/page id map that a T3 (document-per-locale)
    connector OWNS (CONNECTOR_FRAMEWORK_DESIGN + SPEC-t3-notion-helpscout).

    T3 systems (Notion, Help Scout Docs, GitBook, WordPress, AEM) have NO native
    per-locale sub-resource: a "translation" is a SEPARATE page the connector
    creates, so the connector must persist the (source, target_locale) ->
    target_id relationship itself. This lightweight, indexed, per-connection table
    is that map — NOT config JSON (which grows unbounded and can't be looked up or
    partially recovered). Every T3 connector shares this ONE table.

    The `status` column carries the reserve-then-fill + reconciliation lifecycle
    (SPEC-t3 state table):
      building   — target created + id reserved, content not yet fully written (a
                   crash leaves a findable partial; the next run PATCHes it, never
                   POSTs a duplicate).
      done       — target created AND filled AND verified.
      orphaned   — a done target was later deleted in the external system (404);
                   the dead id is never resurrected — a fresh target + new row
                   replace it (the old row is retained for the audit trail).
      needs_link — a target for this (source, locale) EXISTS in the external system
                   but this map never created it (a pre-existing human translation
                   or a lost map). Fail-closed: the connector NEVER overwrites or
                   auto-adopts it; an operator adopts (-> done) or ignores (-> row
                   deleted). This is T3's load-bearing guard against clobbering
                   hand-written translations.

    Uniqueness is (connection_id, source_id, target_locale): exactly one row per
    localized document, so a resumable multi-batch build can never create two."""

    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="connection.id", index=True)
    source_id: str = Field(index=True)  # the source page/doc id in the external system
    target_locale: str  # the locale this row localizes into
    target_id: str | None = None  # the external id of the created target (NULL only transiently)
    status: str = "building"  # building | done | orphaned | needs_link
    updated_at: datetime = Field(default_factory=_utcnow)


class LanguageSet(SQLModel, table=True):
    """A reusable, named set of target locales (source → many targets). Apply it to
    a connection (or project) so ONE request fans out to every language in the set."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    source_locale: str = "en-US"
    target_locales: list | None = Field(default=None, sa_column=Column(JSON))
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


# --- content routing (MCP Phase B) ---
class RoutingRule(SQLModel, table=True):
    """A deterministic rule mapping inbound content (by file extension and, later,
    subject matter) to a translation target — project, provider, workflow, etc.

    Resolution (app/services/routing.resolve) is by `priority` ascending (lower =
    higher precedence), then `id`; the first ACTIVE rule whose `match_ext` and
    `department` match wins. Blank `match_ext`/`department` = matches anything.
    `match_subject` ships but is UNUSED in v1 (Dimension 2 — subject-matter
    classification — is deferred; the column ships so the future classifier needs
    no migration). Targets are all optional; a rule may set only a provider, only a
    project, etc."""

    id: int | None = Field(default=None, primary_key=True)
    priority: int = 100  # lower = higher precedence
    active: bool = True
    match_ext: str = ""  # "srt" or "srt,vtt"; blank = any
    match_subject: str = ""  # reserved for v2, unused in v1
    department: str = ""  # blank = any
    project_id: int | None = Field(default=None, foreign_key="project.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    workflow_id: int | None = Field(default=None, foreign_key="workflow.id")
    language_set_id: int | None = Field(default=None, foreign_key="languageset.id")
    provider: str | None = None
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class SpendLedger(SQLModel, table=True):
    """Accumulated real LLM spend for one (period) window — the global spend-cap
    kill switch's durable store (app/services/spend.py).

    ONE row per period key (currently the calendar year-month "YYYY-MM"), holding
    the running USD total of every priced provider call in that window. A dedicated
    tiny table (rather than reusing Setting rows) is the right shape: spend is a
    monotonic float we UPDATE under a lock on a hot path (every real provider call),
    the period key is a natural primary key, and keeping it out of the Setting
    key/value store avoids churning the settings read-through cache + its
    apply_to_config() side effect on every translation. Rows are never deleted
    (cheap history); enforcement only ever reads the CURRENT period's row.

    `period` is the primary key ("2026-07"); `accumulated_usd` is the sum of
    pricing.cost_usd(...) over the window. `updated_at` is advisory (last write)."""

    period: str = Field(primary_key=True)  # "YYYY-MM" (spend_period="month")
    accumulated_usd: float = 0.0
    updated_at: datetime = Field(default_factory=_utcnow)


class DepartmentBudget(SQLModel, table=True):
    """A per-department monthly translation-token cap (MCP Phase B chunk 4).

    `monthly_token_limit` is the ceiling on tokens_in+tokens_out consumed by
    translate_document jobs attributed to `department` within the current calendar
    month (app/services/budgets). A department with no row — or a row whose limit is
    <= 0 — is UNLIMITED (existing installs see no change). One row per department
    (enforced in the admin CRUD, not the schema, mirroring the app's other tables)."""

    id: int | None = Field(default=None, primary_key=True)
    department: str = ""  # the department this cap applies to
    monthly_token_limit: int = 0  # tokens/month; <= 0 = unlimited
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectBudget(SQLModel, table=True):
    """A per-PROJECT monthly spend cap in MONEY (requirement CV-2).

    Where DepartmentBudget caps translate_document TOKENS per department, this caps
    the USD SPEND of a single project per calendar month — the money picture the
    token cap cannot see. `monthly_usd_limit` is the ceiling on the project's
    ENFORCED spend this month (app.services.project_budgets); a project with no row —
    or a row whose limit is <= 0 — is UNLIMITED (existing installs see no change).
    ONE row per project (a UNIQUE `project_id`), enforced by the schema (unlike
    DepartmentBudget's one-per-department, which the CRUD enforces).

    ENFORCED spend (gated at 100%) = AI model spend (translate/connector Jobs +
    workflow StepRun.cost_usd) + SETTLED vendor human spend (actual_cost_usd of
    returned/reconciled legs), attributed to this project (document -> project) in
    the current calendar month. COMMITTED-but-unpaid vendor spend (open estimates /
    approved quotes on in-flight legs) is DISPLAYED as a second meter marker but is
    NOT enforced against — an overridable maintainer-default (a commitment is not a bill).

    THRESHOLD ALERTS (maintainer-default bands 80% + 100%): crossing a band writes a
    `budget.threshold` audit row and emails the admins ONCE per crossing per month.
    `notified_period` ("YYYY-MM") + `notified_pct` (the highest band, 0/80/100,
    already alerted for that period) persist the crossing state so a burst of
    submissions cannot re-send — the state resets when the period rolls over."""

    # L2: a NAMED unique CONSTRAINT + a NON-unique index on project_id, matching the
    # alembic migration's shape EXACTLY (Field(unique=True) instead emits a unique INDEX,
    # which the migration does not, making the two build paths' index sets diverge). The
    # constraint still guarantees one budget per project; the index keeps get_project_budget
    # a cheap single-row lookup.
    __table_args__ = (UniqueConstraint("project_id", name="ux_projectbudget_project_id"),)
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    monthly_usd_limit: float = 0.0  # USD/month; <= 0 = unlimited
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # Threshold-alert crossing state (once-per-crossing-per-month; see docstring).
    notified_period: str | None = None  # "YYYY-MM" the notified_pct applies to
    notified_pct: int = 0  # highest band alerted this period (0/80/100)
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProjectAutomation(SQLModel, table=True):
    """A project's content source and its run schedule (project automation, MVP part A).

    ONE row per project (UNIQUE project_id). It binds the project to a connection (the
    place its content is pulled from and pushed back to) and carries the schedule:
    UTC times of day and days of week, OR an interval, in delta or full mode, plus the
    pause state, the consecutive-failure counter and the optional per-run USD cap.

    ONE connection may feed MANY projects. `connection_id` is the connection the person
    picked (the credential holder). `run_connection_id` is the connection the runs actually
    use: the same row when the picked connection feeds only this project (project and
    connection already belong together), otherwise a private "lane" clone (Connection.config
    `lane_of` names the picked connection) so each project keeps its own content hashes,
    watermarks and id map and two projects can never overwrite each other's state.
    Write-back ownership of an item is a ProjectBindingClaim row.

    Nothing here enables a schedule by itself: `enabled` is False until a person turns it on,
    and turning it on never fires immediately (the first run is the next slot)."""

    __table_args__ = (UniqueConstraint("project_id", name="ux_projectautomation_project_id"),)
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    connection_id: int = Field(foreign_key="connection.id", index=True)
    run_connection_id: int = Field(foreign_key="connection.id", index=True)
    enabled: bool = False
    mode: str = "delta"  # delta | full
    times: list | None = Field(default=None, sa_column=Column(JSON))  # ["HH:MM", ...] UTC
    days: list | None = Field(default=None, sa_column=Column(JSON))  # [0..6], Monday = 0
    interval_minutes: int = 0
    next_at: datetime | None = None  # naive UTC
    last_at: datetime | None = None
    last_job_id: int | None = None
    last_status: str = ""  # ok | failed | skipped | queued | ""
    last_message: str = ""
    consecutive_failures: int = 0
    auto_pause_after: int = 3
    paused_at: datetime | None = None
    paused_reason: str = ""
    run_cap_usd: float | None = None  # per-run USD cap; None = no cap
    scope: dict | None = Field(default=None, sa_column=Column(JSON))  # per-project scope keys
    # Full pull cursor: item ids already fully written in the CURRENT full cycle, so a scope
    # larger than the per-run write ceiling is walked to the end over several runs.
    full_cursor: list | None = Field(default=None, sa_column=Column(JSON))
    # Signature (owner project -> pairs refused) of the claim block this project was last told
    # about; "" = not blocked. A notification goes out when it CHANGES, not on every blocked run.
    blocked_sig: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class CustomFieldDef(SQLModel, table=True):
    """A customer- or org-defined typed field for projects (SPEC-project-automation
    section 4, MVP part C).

    Scope follows the ContentType idiom: nullable `org_id` + nullable `customer_id`
    (NULL customer = offered for every customer of the org). `key` is the immutable
    slug the CLI, /api and MCP address the field by, and it is unique per SCOPE (org,
    customer; case-folded). The DB enforces that with a FUNCTIONAL unique index over
    (coalesce(org_id,-1), coalesce(customer_id,-1), entity, lower(key)), the same
    single-tenant-safe shape as ux_contenttype_scope_name; the service also refuses a
    global field and a customer field sharing a key, so a project's `custom_fields`
    mapping can never hold two fields under one key.

    `type` is one of the custom_fields.TYPES; `options_json` holds the allowed values
    of a `select`; `key_pattern` is an optional regular expression an `external_link`
    key (or a `text` value) must match; `link_system` names the external system of an
    `external_link` (for example "jira"), the seam phase 3 uses to auto-fill the
    field. A definition that has values is ARCHIVED (`archived_at`), never deleted."""

    __table_args__ = (
        Index(
            "ux_customfielddef_scope_key",
            text("coalesce(org_id, -1)"),
            text("coalesce(customer_id, -1)"),
            text("entity"),
            text("lower(key)"),
            unique=True,
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id", index=True)
    entity: str = "project"
    key: str
    label: str
    type: str = "text"
    options_json: str = ""
    key_pattern: str = ""
    link_system: str = ""
    help_text: str = ""
    required: bool = False
    show_in_list: bool = False
    filterable: bool = True
    list_order: int = 0
    archived_at: datetime | None = None
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProjectBindingClaim(SQLModel, table=True):
    """Write-back ownership of ONE external item (project automation, MVP part A).

    When one connection feeds several projects, the first project whose run reaches an item
    owns it: later projects skip it (and log the conflict) instead of translating and writing
    it over the owner's copy. Keyed on the PICKED connection (ProjectAutomation.connection_id),
    so every project bound to that connection shares one claim space."""

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "external_id", "locale", name="ux_projectbindingclaim_item"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="connection.id", index=True)
    external_id: str = Field(index=True)
    locale: str = ""  # the target locale the claim covers ("" = the whole item)
    project_id: int = Field(foreign_key="project.id", index=True)
    # The project whose last complete run was refused this pair because of this claim (it is the
    # one a hand-over would give the pair to). NULL = nobody is waiting for it.
    wanted_by: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectFieldValue(SQLModel, table=True):
    """One project's value for one CustomFieldDef. Typed storage so list filters and
    sorts are plain indexed comparisons on SQLite and Postgres alike: `value_text`
    (text, long_text, url, select, and the link URL), `value_num` (number),
    `value_date` (date), `value_json` (an external_link's {url,label,system,key}).
    Unique per (project, field)."""

    __table_args__ = (
        UniqueConstraint("project_id", "field_id", name="ux_projectfieldvalue_project_field"),
        Index("ix_projectfieldvalue_field_key", "field_id", "value_key"),
        Index("ix_projectfieldvalue_field_num", "field_id", "value_num"),
        Index("ix_projectfieldvalue_field_date", "field_id", "value_date"),
    )
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    field_id: int = Field(foreign_key="customfielddef.id", index=True)
    value_text: str | None = None
    #: lower(value_text), so an equality filter is an indexed comparison on both databases.
    value_key: str | None = None
    value_num: float | None = None
    value_date: datetime | None = None
    value_json: str | None = None
    updated_by: str = ""
    updated_at: datetime = Field(default_factory=_utcnow)


# --- Evaluations Lab (PROPOSAL-evaluations-lab section 4) ----------------------
# SIX new tables. They are the whole isolation story: "the Lab owns its own tables"
# (proposal section 3, rule 1) means production queries never join them, so there is
# no production query to get wrong and no `is_sandbox` boolean threaded through the
# hot paths. Project/Document/Segment/TM/LQAResult are UNTOUCHED by this feature.
#
# Alembic revision c5a1f8e3b724 creates all six. No app/db.py `_ADDED_COLUMNS`
# entry: that shim exists for new COLUMNS on EXISTING tables (which create_all()
# will never add). These are new TABLES, which create_all() DOES create on a
# non-alembic SQLite boot -- the same posture as workflowgroup / contenttype /
# styleguide / memoryentry (revision b3e7c1d94a26 documents it at length).
class EvalOrigin:
    """How an EvalDataset's items got here.

    UPLOAD               - pasted/uploaded text (source, optionally source+reference).
    COPIED_FROM_DOCUMENT - SNAPSHOT-copied out of a production Document. The text was
                           COPIED; `EvalDataset.source_document_id` records WHICH
                           document as a bare integer and nothing more (see below)."""

    UPLOAD = "upload"
    COPIED_FROM_DOCUMENT = "copied_from_document"
    ALL = (UPLOAD, COPIED_FROM_DOCUMENT)


class EvalRunStatus:
    """Lifecycle of one (dataset x candidate x repeat) execution.

    STOPPED is the budget-ceiling outcome (proposal section 12.2): the run halted
    CLEANLY with PARTIAL results rather than silently burning through the dataset. It
    is a distinct state from FAILED precisely so a surface can say "stopped at your
    ceiling, here is what we got" instead of "error"."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    ALL = (PENDING, RUNNING, DONE, FAILED, STOPPED)
    TERMINAL = (DONE, FAILED, STOPPED)


class EvalScoreState:
    """WHY A SCORE HAS A STATE COLUMN AT ALL (proposal section 6, the design-critical
    rule).

    "No reference available" and "scored badly" are DIFFERENT STATES, and conflating
    them destroys trust in the whole panel. A BLEU of 0.0 means the output shares no
    n-grams with the reference; a BLEU that could not be computed because
    `EvalDatasetItem.reference_text` is NULL is not a 0 and is not a blank -- it is
    NOT_APPLICABLE, and every surface must render it as "no reference" (or hide the
    column) rather than as a number.

    Making this a first-class column rather than a convention means the distinction
    survives the round-trip through the database. `value` is NULL for every state
    except OK, so there is no number lying around for a careless AVG() to pick up.

      OK             - the scorer ran; `value` is a real number.
      NOT_APPLICABLE - the scorer declined (needs_reference and no reference).
                       `value` IS NULL. Never averaged, never ranked, never charted.
      ERROR          - the scorer raised; `detail["error"]` says what. `value` IS NULL.
    """

    OK = "ok"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"
    ALL = (OK, NOT_APPLICABLE, ERROR)


class EvalDataset(SQLModel, table=True):
    """A named collection of source (and optionally reference) segments the Lab runs
    candidates against.

    `source_document_id` IS A PROVENANCE INTEGER, NOT A FOREIGN KEY -- deliberately,
    and this is proposal section 3, rule 3 made structural. Copy-in is a SNAPSHOT: the
    text is COPIED into EvalDatasetItem rows so that editing, re-translating or
    ordinarily deleting the production document afterwards cannot change an experiment's
    inputs (a result you cannot reproduce is not a result). An FK would create exactly
    the live coupling the isolation design forbids -- ON DELETE would either cascade a
    production delete into the Lab or block a production delete on a Lab row, and
    either way the dataset would stop being a snapshot. So: a bare integer, no FK, no
    relationship. Privacy erasure and scheduled retention consult this provenance to
    delete the snapshot and derived results explicitly. NULL for uploaded datasets.

    `customer_id` IS nullable here (unlike MemoryEntry's hard partition): a Lab dataset
    is often a synthetic or uploaded corpus that belongs to no customer. Scoping for
    RBAC is by org_id/customer_id where set, applied by the surfaces via access.py."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    description: str = ""
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id", index=True)
    source_locale: str = "en-US"
    target_locale: str = ""
    origin: str = EvalOrigin.UPLOAD
    # Provenance for privacy erasure and opt-out; never dereferenced for source content.
    source_document_id: int | None = None
    # AI opt-out (HECVAT DPAI-08): the snapshot's SOURCE project and customer, stamped at
    # snapshot time. Bare integers like source_document_id (provenance, not FKs), so the
    # opt-out check survives ordinary document/project deletion. Privacy erasure instead
    # uses this provenance to remove the snapshot. A missing source row, or one created AFTER
    # the dataset (a reused id), fails closed in ai_optout.refuse_for_eval_dataset.
    source_project_id: int | None = None
    source_customer_id: int | None = None
    item_count: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: str = ""


class EvalDatasetItem(SQLModel, table=True):
    """One evaluated unit: a source segment plus an OPTIONAL reference translation.

    `reference_text` IS NULLABLE AND THAT IS THE POINT (proposal section 6). Most real
    content has no reference, so reference-availability is a first-class property of
    the data model rather than an edge case bolted on later. Reference-based scorers
    (BLEU/chrF/TER/edit-distance/COMET) return NOT_APPLICABLE for these rows;
    reference-free scorers (deterministic QA checks, the LQA/QE judges) are the default
    path and run on every row. The section-6 escape hatch -- promote a candidate's
    output, or a human edit of it, to BE the reference -- is just an UPDATE of this
    column, which retroactively unlocks the stronger metrics.

    `item_index` (not `index`, a SQL keyword) preserves document order for display and
    for stable per-segment pairing across candidates -- the paired bootstrap in
    app/services/eval_stats.py is only valid if candidate arrays line up item for item,
    so a stable order is load-bearing, not cosmetic.

    `meta` (not `metadata`, which SQLAlchemy's declarative layer reserves) carries
    domain / content-type / any per-item annotation a surface wants to filter on."""

    id: int | None = Field(default=None, primary_key=True)
    dataset_id: int = Field(foreign_key="evaldataset.id", index=True)
    item_index: int = 0
    source_text: str = ""
    # NULLABLE ON PURPOSE. NULL = "no reference", which is NOT "empty reference".
    reference_text: str | None = None
    meta: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class EvalCandidate(SQLModel, table=True):
    """A dataset-independent CONFIG UNDER TEST: a workflow, or an inline snapshot of
    provider/model/prompt/context toggles.

    IMMUTABLE ONCE RUN AGAINST. `locked` is stamped the moment an EvalRun references
    this candidate; from then on an "edit" through evaluations.update_candidate()
    FORKS -- it writes a NEW row with `derived_from_id` pointing back -- rather than
    mutating this one. The reason is the entire value proposition of the Lab: a result
    must always name the EXACT configuration that produced it. If a candidate could be
    edited after a run, every historical run would silently start misdescribing itself,
    and the comparison view would be quietly lying.

    `config` is a SNAPSHOT of the inline configuration, not a pointer, for the same
    reason. `workflow_id` IS an FK (a Lab-scoped Workflow is a real production-shaped
    object the promotion door clones), but a run also copies the resolved
    provider/model onto the EvalRun so the record survives later workflow edits."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    description: str = ""
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id", index=True)
    workflow_id: int | None = Field(default=None, foreign_key="workflow.id")
    config: dict | None = Field(default=None, sa_column=Column(JSON))
    # True once any EvalRun references this candidate. Edits fork instead of mutate.
    locked: bool = False
    # The candidate this one was forked from (self-FK, nullable, set on fork only).
    derived_from_id: int | None = Field(default=None, foreign_key="evalcandidate.id")
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: str = ""


class EvalRun(SQLModel, table=True):
    """One (dataset x candidate x repeat_index) execution. IMMUTABLE once terminal.

    `repeat_index` is the repeat-N axis (proposal section 7): LLM nondeterminism is a
    second variance source classic MT statistics never modeled, so the same pair can be
    run N times and the spread across repeats is itself a reported quantity.

    `metric_signature` is the provenance blob (section 6): for each scorer that ran,
    its id, version, the sacreBLEU SIGNATURE STRING and the tokenizer used (plus, for
    Tier 1, which model judged). Unlabeled scores are not comparable -- that is the
    entire reason sacreBLEU exists -- so the signature travels with the run, not in a
    wiki.

    `cost_usd`/`tokens_*` are the REALIZED spend, recorded beside the pre-run estimate
    so estimate-vs-actual divergence is visible (section 12.3). `budget_usd` is the
    per-experiment ceiling; 0 means unlimited. Tripping it ends the run STOPPED with
    partial results, never FAILED and never silently."""

    id: int | None = Field(default=None, primary_key=True)
    dataset_id: int = Field(foreign_key="evaldataset.id", index=True)
    candidate_id: int = Field(foreign_key="evalcandidate.id", index=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    status: str = EvalRunStatus.PENDING
    repeat_index: int = 0
    # The scorer ids selected for this run (list[str]); the surfaces enumerate from
    # app/services/eval_metrics.py rather than hardcoding, so this is just a record.
    scorers: list | None = Field(default=None, sa_column=Column(JSON))
    metric_signature: dict | None = Field(default=None, sa_column=Column(JSON))
    provider: str = ""
    model: str = ""
    item_count: int = 0
    cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    budget_usd: float = 0.0  # 0 = no ceiling
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: str = ""


class EvalRunItem(SQLModel, table=True):
    """One produced output inside a run.

    `output_text` lives HERE and ONLY here. Proposal section 3, rule 2: a Lab run NEVER
    writes Segment.target_text, segment status, qa_flags, TM, LQAResult or LLMemory --
    it scores what it produced in its own table and the production side never learns
    the text existed. This column is the only place a Lab output is durable."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="evalrun.id", index=True)
    dataset_item_id: int = Field(foreign_key="evaldatasetitem.id", index=True)
    output_text: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class EvalScore(SQLModel, table=True):
    """One scorer's verdict on one run item -- or, when `run_item_id` is NULL, on the
    whole run (a corpus-level aggregate such as corpus BLEU).

    `run_id` is ALWAYS set, including on per-item rows, so "every score in this run" is
    one indexed filter instead of a join through evalrunitem.

    `state` + nullable `value` together implement proposal section 6 (see
    EvalScoreState): a NOT_APPLICABLE row has value NULL, so a missing reference can
    never be averaged in as a zero by a careless query. `detail` carries the provenance
    and the explanation -- sacreBLEU signature, tokenizer, judge reasoning, error
    spans, the QA check's reason string -- because a bare number is never enough to act
    on."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="evalrun.id", index=True)
    # NULL = a run-level AGGREGATE score rather than a per-item score.
    run_item_id: int | None = Field(default=None, foreign_key="evalrunitem.id", index=True)
    scorer: str = Field(default="", index=True)
    state: str = EvalScoreState.OK
    # NULL whenever state != OK. "No reference" is not a zero.
    value: float | None = None
    detail: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


# --- Prompt Tap run envelopes (PROPOSAL-prompt-tap-run-envelopes, Wave A) ------
class TapActionKind:
    """How a TapAction was initiated (proposal section 3).

    WORKFLOW - one run of a Workflow over a (document, locale). Carries workflow id +
               name + step-graph fingerprint; a batch member also carries batch_id.
    ADVANCED - one Advanced mission execution, with its own language-pair run leg.
    DIRECT   - a non-workflow provider call: the plain per-segment MT path
               (translate.pretranslate_stream) or ad-hoc translate_text. A minimal
               auto-action so that EVERY captured call belongs to exactly one action
               (proposal section 3, "everything that called a provider belongs to
               exactly one action") -- never an "ungrouped" bucket.

    Eval Lab traffic is DELIBERATELY NOT an action: those captures carry eval_* meta
    and NO action_id, so Lab runs stay distinguishable from production (proposal
    section 3). No TapAction is ever created on the eval path."""

    WORKFLOW = "workflow"
    DIRECT = "direct"
    ADVANCED = "advanced"
    ALL = (WORKFLOW, DIRECT, ADVANCED)


class TapStatus:
    """Lifecycle of an envelope (TapAction / TapRun). APPEND-ONLY semantics: an
    envelope's recorded FACTS are never edited after the fact; finalization is a
    STATUS TRANSITION (running -> completed/failed/abandoned), never a rewrite of
    history (proposal section 4, "append-only events"). PAUSED mirrors a WorkflowRun
    that paused for human review; the resume that finishes it transitions it onward.

    ABANDONED is a distinct TERMINAL state from FAILED (M1/M2): the run was DROPPED, not
    errored. Two honest sources -- the consumer closed the stream mid-run (SSE client
    disconnect) before the work emitted its own summary / terminal event; OR the run was
    CANCELLED by an operator (orchestrator.cancel_run), which reaches a terminal production
    status the run's own `finally` then maps to ABANDONED (fix/tap-finalize-race). Either
    way the run did not FAIL (no error), it was simply dropped -- and this keeps a dropped
    run from being stuck 'running' forever.

    ORPHANED is the REAPER's terminal marker (residual sweep: "no reaper for orphaned
    actions"). An envelope left RUNNING by a crash / kill / thread-death -- the case the
    _revive_action_if_terminal / TapAction.finished_at comments call out as unhandled --
    is closed by tap_envelope.reap_orphaned_envelopes once it is PROVABLY dead (aged past
    the threshold AND no live backing production run). Distinct from ABANDONED (a live
    consumer dropped the stream) and FAILED (the work errored): ORPHANED says "the process
    that owned this envelope died without finalizing it", so a reader is told the honest
    truth instead of seeing a perpetual 'running'. It is a status TRANSITION, never a
    delete -- the envelope hierarchy + TTL semantics are preserved."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    ABANDONED = "abandoned"
    ORPHANED = "orphaned"
    ALL = (RUNNING, COMPLETED, FAILED, PAUSED, ABANDONED, ORPHANED)
    TERMINAL = (COMPLETED, FAILED, ABANDONED, ORPHANED)


# The envelope schema version stamped on every TapAction / TapRun row AND every call
# record from day one (proposal section 4, "a schema_version on every envelope and
# call record"). Bumped only on a breaking shape change so an exporter can branch.
TAP_SCHEMA_VERSION = 1


class TapAction(SQLModel, table=True):
    """ONE translate action -- the top of the run-envelope hierarchy (proposal
    section 3): a workflow run over a (document, locale), a direct-MT pass, or an
    ad-hoc translate_text call. The per-language-pair legs hang off it as TapRun
    rows; the individual provider calls stay file-based, each stamped with this
    row's id (meta.action_id) plus its run's id (meta.run_id).

    SCOPE (Wave A): an action wraps a TRANSLATE run only -- workflow runs, direct MT,
    and ad-hoc translate_text. Utility provider calls that are NOT translate runs
    (prompt authoring, LLMemory distillation, setup_author, assist) are DELIBERATELY
    unstamped in Wave A: they belong to no action and their captures stay in the legacy
    global file. Eval Lab calls are likewise deliberately unstamped (eval_* meta, no
    action_id -- section 3). Advanced eval arm E uses `ledger_envelope=False`: its
    legacy UUID/advrun capture identifiers belong to no envelope rows.
    So "every provider call belongs to an action" holds for
    translate traffic, which is what the envelope hierarchy is about.

    STRING ULID PRIMARY KEY, not an autoincrement integer (proposal section 4,
    "stable ids (ULIDs) so exports join cleanly"): ULIDs are time-sortable and
    index-friendly, and they are also the per-action CALL FILE name
    (prompt_tap/actions/<id>.jsonl), so they are validated as a filename-safe token
    before that path is built.

    `document_id` is NULLABLE (proposal section 11.1): a key-set project run carries
    key-set identity instead of a document id -- one shape, nullable doc/key fields.

    `manifest` and `totals` are NULLABLE JSON columns reserved for WAVE B (asset
    manifest: TM/glossary/style/LLMemory ids+hashes; and honest roll-up totals with
    their raw parts). They stay NULL in Wave A so Wave B enrichment bolts on with NO
    schema churn -- the whole point of putting the extensible payload in JSON now."""

    id: str = Field(primary_key=True)
    # Stamped on every row so an exporter can branch on shape (section 4).
    schema_version: int = TAP_SCHEMA_VERSION
    kind: str = TapActionKind.DIRECT
    # Who/what initiated (WorkflowRun.created_by / the actor). Best-effort attribution.
    initiator: str = ""
    # Best-effort surface tag (web|cli|api|mcp|batch|connector|""). Not authoritative
    # in Wave A -- a hint for the Wave C viewer; never a security decision.
    surface: str = ""
    # ALL of these are PROVENANCE INTEGERS, NOT FOREIGN KEYS -- deliberately, and this is
    # the same decision the eval tables made for EvalDataset.source_document_id. An
    # envelope is a PASSIVE, APPEND-ONLY OBSERVER of a run: it records WHICH project /
    # document / workflow / batch / org / customer a run was against, but it must never
    # be able to BLOCK or CASCADE the deletion of one of those production rows. A real FK
    # here would do exactly that -- deleting a WorkflowRun / Project / Document with an
    # envelope row pointing at it would be refused by the FK (observed: verify_cli /
    # verify_jobs_history delete runs a translation had captured). So these are bare ints
    # (indexed where section 6 requires); a reader who greps for a missing FK finds this.
    org_id: int | None = None
    customer_id: int | None = None
    project_id: int | None = Field(default=None, index=True)
    # NULLABLE: a key-set run has no single document (section 11.1).
    document_id: int | None = Field(default=None, index=True)
    source_locale: str = ""
    # The target locales THIS action requested (a JSON list). ONE entry for a single
    # workflow / direct run; ALL of a batch click's locales for a batch action -- the
    # shipped grain (D1) is ONE TapAction per translate CLICK, so a batch that fanned out
    # to N language pairs is that ONE action with N TapRuns (never one action per locale).
    requested_locales: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    workflow_id: int | None = None
    workflow_name: str = ""
    # A light fingerprint of the step graph (keys+types) so a viewer/export can tell
    # two runs of "the same workflow" apart after the workflow was edited. "" for a
    # direct action.
    step_fingerprint: str = ""
    # Non-NULL links this action to the WorkflowBatch whose ONE click this action
    # records (D1): a project-wide/multi-locale batch is ONE TapAction (this one) with a
    # TapRun per launched language pair -- the batch_id is that action's identity, and
    # every member leg attaches its TapRun to the single action carrying this batch_id.
    # Provenance integer, NOT an FK, and NOT unique/enforced: WorkflowBatch.id is a rowid
    # alias that is REUSED after a batch is deleted, so a lookup by batch_id must validate
    # the action's tenant identity on read (tap_envelope.find_batch_action, finding B1).
    batch_id: int | None = Field(default=None, index=True)
    status: str = Field(default=TapStatus.RUNNING, index=True)
    started_at: datetime = Field(default_factory=_utcnow, index=True)
    # A re-run RE-OPENS a terminal batch action to 'running'
    # (tap_envelope._revive_action_if_terminal) so its re-dispatched legs re-roll. If that
    # re-run's daemon thread is killed mid-flight (a server restart -- the known uvicorn
    # reload hazard), the action is left 'running'; finished_at keeps the LAST-KNOWN finish
    # time (a re-completion re-anchors it). Such an aged, backing-dead RUNNING row is now
    # closed by tap_envelope.reap_orphaned_envelopes (the ORPHANED terminal marker) on the
    # scheduler tick; the batch's OWN members are still healed by
    # batch_runs.recover_interrupted_batches.
    finished_at: datetime | None = None
    # DISK-BUDGET BACKSTOP (proposal section 7, pulled forward from Wave C). Flipped True
    # when new per-call capture for this action was WITHHELD because prompt_tap/actions/
    # exceeded prompt_tap_actions_max_bytes -- the envelope keeps recording, but its
    # call file stops growing, and this flag says so plainly rather than the volume being
    # silently eaten (the incident lesson applied in advance). NOT NULL, defaults False.
    capture_withheld: bool = False
    # WAVE B (reserved, NULL in Wave A): the asset manifest -- see the class docstring.
    # none_as_null=True so a Python None persists as SQL NULL (not JSON 'null'), so Wave
    # B's `WHERE manifest IS NULL` predicate is correct.
    manifest: dict | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    # WAVE B (reserved, NULL in Wave A): roll-up totals, always stored beside the raw
    # parts they sum (section 4, "no derived value without the raw values").
    totals: dict | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    created_at: datetime = Field(default_factory=_utcnow)


class TapRun(SQLModel, table=True):
    """ONE language-pair leg of a TapAction (proposal section 3): en->de-DE, en->fr-FR.
    A workflow run over a (document, locale) is exactly one TapRun; a BATCH of N locales
    is ONE TapAction with N TapRuns (one per language-pair leg), NOT one action per leg
    (the shipped D1 grain). The provider calls of this leg are the file-based records
    stamped meta.run_id == this row's id.

    `workflow_run_id` is the JOIN KEY back to the production WorkflowRun (NULLABLE:
    a direct-MT leg has no WorkflowRun). It is what lets a RESUME reuse the same
    envelope instead of forking a duplicate, and what the finalize seam looks the
    row up by when it reads the run's terminal status. Indexed for both.

    `detail` is a NULLABLE JSON column reserved for WAVE B (per-leg TM/glossary/engine
    detail + roll-ups) -- NULL in Wave A, so Wave B bolts on with no schema churn."""

    id: str = Field(primary_key=True)
    schema_version: int = TAP_SCHEMA_VERSION
    # action_id IS a real FK -- it is INTERNAL (envelope -> envelope), a parent that is
    # created immediately before this row and deleted only by the guarded envelope purge
    # (Wave C), never by a production delete. It is the one reference safe to enforce.
    action_id: str = Field(foreign_key="tapaction.id", index=True)
    # PROVENANCE INTEGER, NOT AN FK (see TapAction's block): the join back to the
    # production WorkflowRun (workflow legs only; NULL for direct MT). A run being
    # deleted (jobs cleanup, retention) must NOT be blocked by an envelope pointing at
    # it -- so this records the id without enforcing it. The resume-reuse lookup and the
    # Wave C viewer both tolerate a run id that no longer resolves.
    workflow_run_id: int | None = Field(default=None, index=True)
    target_locale: str = ""
    source_locale: str = ""
    status: str = Field(default=TapStatus.RUNNING, index=True)
    started_at: datetime = Field(default_factory=_utcnow, index=True)
    finished_at: datetime | None = None
    # WAVE B (reserved, NULL in Wave A): per-leg enrichment -- see the class docstring.
    # none_as_null=True so None persists as SQL NULL (Wave B keys on IS NULL).
    detail: dict | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    created_at: datetime = Field(default_factory=_utcnow)


# --- Vendors + external dispatch-and-wait (PROPOSAL-external-processing-step §9,
#     PROPOSAL-vendor-human-review-handoff; combined build Wave A) ---------------
class VendorStatus:
    """Connection lifecycle of a Vendor, mirroring ConnStatus. NEW until a dispatch
    or explicit test succeeds; ERROR on a failed dispatch. Purely a UI/diagnostic
    hint — it never gates dispatch (fail-closed vendor resolution is by customer
    link + endpoint validity, not this field)."""

    NEW = "new"
    CONNECTED = "connected"
    ERROR = "error"


class Vendor(SQLModel, table=True):
    """A first-class external translation/review VENDOR (decision 6, the maintainer near-
    verbatim): "a new side panel: add any number of vendors, name them, enter the
    API connector URL/secret/etc. to establish the connection on our end. A vendor is
    connectable to any number of customers, and vendors are the selectable options on
    a workflow's human-review/external step."

    A vendor owns:
      - the CONNECTION: `endpoint_url` (outbound dispatch target, SSRF-guarded at
        dispatch time via connectors.base._validate_target) + a `secret_enc` Fernet
        blob (the shared signing secret + any auth token — stored EXACTLY like a
        connector's credentials, never echoed to any surface or run envelope);
        `config` holds non-secret transport settings (method, auth_mode, headers).
      - the MSA COST MODEL (decision 5a): `rate_card`, a per-TM-match-band cost-per-
        word table conforming with the vendor's MSA — see app/services/vendor_cost.py
        for the band vocabulary (aligned to XLIFF state-qualifier names so the vendor
        XLIFF profile slots in later without a redesign).
      - its customer links (the VendorCustomer join table): a vendor is offered on a
        step only within a customer it is linked to (fail-closed).

    ISOLATION: `org_id` is the OUTER multi-org boundary; the customer link is the
    per-customer scoping layer under it (same shape as Connection.customer_id).
    `archived_at` retires a vendor from every picker without breaking existing
    dispatch history (the Customer.archived_at precedent)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    description: str = ""
    # Outbound dispatch endpoint (the vendor's intake URL). Validated by the SSRF
    # guard at dispatch time, never trusted as-stored.
    endpoint_url: str = ""
    # Non-secret transport config: {"method": "POST", "auth_mode": "hmac"|"bearer",
    # "headers": {...}, ...}. NULL = defaults (POST + HMAC signing).
    config: dict | None = Field(default=None, sa_column=Column(JSON))
    # Fernet ciphertext of the credentials JSON ({"signing_secret": "...",
    # "auth_token": "..."}) — never plaintext, never echoed. Same pattern as
    # Connection.secret_enc / EngineProfile.api_key_enc.
    secret_enc: str = ""
    # MSA rate card: {"currency": "USD", "bands": {<band>: <cost_per_word>, ...}}.
    # NULL = the built-in zero-rate default (vendor_cost.default_rate_card()).
    rate_card: dict | None = Field(default=None, sa_column=Column(JSON))
    status: str = VendorStatus.NEW
    last_message: str = ""  # last dispatch/test result, for the UI
    # TRANSPORT PROTOCOL seam (interop R1, Wave C — seam only). Names the transport
    # implementation a dispatch/return uses; `native` is today's exact behavior (our
    # signed JSON contract over webhook/poll/manual) and the ONLY registered protocol.
    # Per-LSP adapters (lionbridge/globallink/trados) are LATER waves, blocked on real
    # sandbox tenants. INVARIANT: a protocol is a TRANSPORT only — every inbound path
    # still funnels through vendor_return.admit_return -> commit_mt_output; no protocol
    # ever gains its own admission path. Validated against vendor_dispatch.PROTOCOLS at
    # vendor create/update (never at dispatch time). NULL/'' on a legacy row reads as
    # 'native' at the single dispatch-by-protocol seam, so pre-Wave-C rows are byte-
    # identical.
    protocol: str = "native"
    archived_at: datetime | None = Field(default=None)
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class VendorCustomer(SQLModel, table=True):
    """The many-to-many link between a Vendor and a Customer (decision 6: "a vendor is
    connectable to any number of customers"). A vendor is only offered/valid on a
    workflow step whose document's customer is linked here — the fail-closed scoping
    seam (vendors.vendor_for_customer). Uniqueness (vendor_id, customer_id) is enforced
    by a unique index (db._UNIQUE_INDEXES + the alembic migration) so a double-link is
    a no-op, never a duplicate row."""

    id: int | None = Field(default=None, primary_key=True)
    vendor_id: int = Field(foreign_key="vendor.id", index=True)
    customer_id: int = Field(foreign_key="customer.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class VendorDispatchStatus:
    """Lifecycle of one VendorDispatch leg. Wave A only ever writes DISPATCHED (the
    leg is parked PAUSED awaiting a return) and ESCALATED (the expiry escalation to a
    human_review pause, decision 3). RETURNED + CANCELLED are Wave B (the guarded
    return intake + abort). RETRY_WAIT + DEAD_LETTER are Wave C (interop R5, the
    outbound-dispatch dead-letter/backoff): a TRANSIENT transport failure parks the leg
    RETRY_WAIT (run paused exactly like a successful dispatch) and the scheduler retries
    it on an exponential backoff; exhausting the bounded window flips it DEAD_LETTER AND
    escalates to human review (decision 3 — vendor unreachability never hard-fails)."""

    DISPATCHED = "dispatched"  # sent; workflow leg parked PAUSED awaiting return
    RETURNED = "returned"  # Wave B: a validated return was ingested
    ESCALATED = "escalated"  # expiry escalated the leg into a human_review pause
    CANCELLED = "cancelled"  # Wave B: aborted/superseded; a late return is dropped
    RETRY_WAIT = "retry_wait"  # Wave C: transient dispatch failure; awaiting a backoff retry
    DEAD_LETTER = "dead_letter"  # Wave C: retry window exhausted; escalated to a human
    # Wave E (R3, quote-approval gate). QUOTED is an OPEN state (like DISPATCHED, it parks
    # the run PAUSED and is swept on expiry / closed on run-cancel) that sits BETWEEN
    # dispatch and work: with the per-vendor `requires_quote_approval` flag ON the leg is
    # recorded QUOTED, the package is sent carrying `quote_requested`, and the leg awaits a
    # vendor/operator QUOTE and then an operator APPROVAL. Approval flips QUOTED -> DISPATCHED
    # (work proceeds; a return can then be admitted); decline escalates to a human_review
    # pause (reusing the escalation body). With the flag OFF a leg is NEVER QUOTED and every
    # existing path is byte-identical.
    QUOTED = "quoted"  # Wave E: awaiting a quote + operator approval before work
    # Wave E (R4, reject/rework). A RETURNED leg an operator rejects becomes REJECTED
    # (terminal for THAT leg) and a NEW rework leg (round N+1, DISPATCHED) is sent to the
    # SAME vendor carrying structured feedback; the rework return funnels through the SINGLE
    # guarded admission seam, never a new path. REJECTED admits nothing.
    REJECTED = "rejected"  # Wave E: a delivered return was rejected; superseded by rework


class VendorDispatch(SQLModel, table=True):
    """One outbound dispatch-and-wait leg: the record the EXTERNAL step writes when it
    packages a (document, locale) unit, computes the scoping estimate, and sends it to
    a vendor (PROPOSAL-external-processing-step §3, decision 5b/6).

    It is the anchor Wave B reconciles against: the `claim_token` matches a signed
    return to the exact run/step/locale (tenant-scoped by the vendor/customer/org chain
    recorded here — a token for one customer's run can never admit another's), and the
    persisted `estimate` (+ `estimate_cost_usd`) is the figure the vendor's returned
    `actual_cost_usd` reconciles against (estimate -> actual, decision 5c).

    SECRET DISCIPLINE: only `endpoint_host` (host, never the full URL or secret) is
    stored — the run envelope / any surface may show where it went, never how it was
    signed.

    ORG/CUSTOMER STAMPING (cross-org linking, 2026-08-08): `org_id`/`customer_id` here
    are the BUYER side — the org and customer of the RUN that dispatched this leg (the
    orchestrator derives org_id from the run's CUSTOMER, never from the vendor). With
    vendor<->customer linking now unrestricted across orgs, the vendor may live in a
    DIFFERENT org than this leg; the leg is deliberately NOT stamped with the vendor's org.
    Consequences, by design: the manual-return / poll paths scope a leg by its BUYER org
    (`admit_manual(org_id=)`, `open_legs(org_id=)`), so a return is serviced within the
    buyer's ring — the tenant boundary for the WORK is the buyer, not the (possibly
    foreign) vendor. The vendor-detail leg list is scoped by `vendor_id` (already gated to
    the viewer's owned vendor), so it is the vendor-admin's window onto its own legs.
    (All org scoping is inert while the multi-org gate is OFF — the production posture.)"""

    id: int | None = Field(default=None, primary_key=True)
    vendor_id: int = Field(foreign_key="vendor.id", index=True)
    workflow_run_id: int = Field(foreign_key="workflowrun.id", index=True)
    step_id: int | None = Field(default=None, foreign_key="workflowstep.id")
    step_key: str = ""  # stable step slug, for the envelope
    document_id: int | None = Field(default=None, foreign_key="document.id")
    target_locale: str = ""
    # The match token — a high-entropy secret persisted here and echoed to the vendor
    # in the contract; Wave B matches a return to THIS leg by it. Indexed for the
    # return lookup; effectively unique (a 32-byte urlsafe token).
    claim_token: str = Field(default="", index=True)
    contract_version: str = ""
    endpoint_host: str = ""  # host only — never the secret/full URL
    status: str = VendorDispatchStatus.DISPATCHED
    segment_count: int = 0
    word_count: int = 0
    # The full scoping-estimate breakdown (per-band word counts + per-band cost +
    # billable/covered split), the vendor_cost.scoping_estimate() dict.
    estimate: dict | None = Field(default=None, sa_column=Column(JSON))
    estimate_cost_usd: float = 0.0  # the headline billable estimate
    estimate_currency: str = "USD"
    # Wave B: the vendor-reported actual cost for this leg (estimate -> actual
    # reconciliation, decision 5c). NULL until a return updates it.
    actual_cost_usd: float | None = Field(default=None)
    # Wave B: the CONTRACT ID LEDGER — `{"dispatched": [...], "returned": [...]}`.
    # `dispatched` is the exact id multiset the contract carried (written once at
    # dispatch); `returned` accumulates the ids ALREADY ADMITTED by earlier partial
    # returns. Both are needed and NEITHER is derivable: the document's segment rows
    # drift after dispatch (a later import may add ids the vendor was never sent), and
    # segment state cannot distinguish "admitted by this return" from "already had a
    # target". Without the ledger a duplicate return would re-admit silently and an
    # id added post-dispatch would be accepted as legitimate. NULL on a Wave-A row =
    # no ledger (the return path treats such a leg as un-accountable and refuses).
    contract_ids: dict | None = Field(default=None, sa_column=Column(JSON))
    dispatched_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = Field(default=None)  # NULL = no bounded wait
    returned_at: datetime | None = Field(default=None)  # Wave B
    escalated_at: datetime | None = Field(default=None)  # decision 3 escalation
    # Wave C dead-letter/backoff (interop R5). A TRANSIENT outbound-dispatch failure
    # parks the leg RETRY_WAIT with the retry count so far and the earliest instant the
    # scheduler may retry it (exponential backoff, RETRY_BACKOFF_SCHEDULE). `last_error`
    # is the SANITIZED reason (no URLs with credentials, no secrets) rendered on the leg
    # panels. All three are 0/NULL on a leg that never failed transiently -> a Wave-A/B
    # row is byte-identical.
    attempt_count: int = 0  # transient-failure retries so far
    next_attempt_at: datetime | None = Field(default=None)  # earliest next retry (RETRY_WAIT)
    last_error: str = ""  # sanitized last transient reason
    # A FROZEN contract DICT (segments + ids + assets), with TWO status-keyed meanings, each
    # its own LIFETIME (single-sourced in vendor_dispatch._clear_or_keep_frozen, which every
    # status transition routes through):
    #   (1) a RETRY_WAIT leg's pending RE-SEND -- so a scheduled retry re-sends the byte-
    #       identical package (re-signed fresh, so a rotated secret is honored). CLEARED
    #       (set NULL) on EVERY exit from RETRY_WAIT (DISPATCHED/QUOTED restore, or any
    #       terminal status). This use carries the REAL claim token (retry re-signs from it).
    #   (2) a delivered WIRE-LESS (`manual`) leg's OPERATOR-DELIVERED package -- frozen at
    #       dispatch so the "Download XLIFF" surface renders it verbatim. The download IS the
    #       delivery for a manual vendor, and rebuilding from the live Segment rows mid-run
    #       yields EMPTY targets (the run's MT draft is in rolling text until it completes;
    #       AUDIT-omegat-roundtrip-2026-08-10). `assets.xliff` is stripped from this frozen
    #       copy (the download re-renders XLIFF anyway) and the CLAIM TOKEN is BLANKED (the
    #       download blanks it anyway, and this package never feeds the re-sign path); every
    #       other asset is kept. It is KEPT while the leg is WORKABLE (DISPATCHED / QUOTED /
    #       ESCALATED -- the operator must still be able to download the correct file) and
    #       CLEARED when the leg reaches a state with no pending delivery (RETURNED / REJECTED /
    #       CANCELLED / DEAD_LETTER). A DOCUMENT DELETE also erases it (2026-08-31):
    #       retention.delete_document clears it + severs document_id on every leg of the
    #       document, and deletes outright any leg anchored to one of that document's runs
    #       (workflow_run_id is NOT NULL) -- the ordinary shape. Until that fix the surrounding
    #       delete was FK-blocked by the document's own runs, so the clear always rolled back.
    # Rebuilding from live Segment rows is exactly the drift `contract_ids` exists to prevent,
    # so the package is STORED, not re-derived. NULL on every NATIVE delivered leg (byte-
    # identical) and every legacy row. Only the RETRY_WAIT re-send path keys on status for
    # RETRY/dead-letter, so a manual DISPATCHED/QUOTED leg carrying a wire-less package is inert
    # in every retry/dead-letter sweep.
    retry_contract: dict | None = Field(default=None, sa_column=Column(JSON))
    # Wave E R3 (quote-approval gate). `requires_quote` snapshots the vendor's
    # `requires_quote_approval` flag at dispatch time so the leg's own machinery (the
    # RETRY_WAIT->delivered restore, the surfaces) knows it is quote-gated even if the
    # vendor config changes while parked. When True the delivered leg is recorded QUOTED
    # (awaiting a quote + operator approval) instead of DISPATCHED. `quoted_cost_usd` is the
    # vendor/operator QUOTE for this leg -- the estimate->QUOTE->actual reconciliation chain
    # sits ALONGSIDE `estimate_cost_usd` (our scoping estimate, untouched) and
    # `actual_cost_usd` (the return's reconciled figure); approving a quote records it, never
    # overwriting the estimate. All 0/NULL/'' on a non-quote-gated leg -> byte-identical.
    requires_quote: bool = Field(default=False)
    quoted_cost_usd: float | None = Field(default=None)
    quote_currency: str = ""
    quoted_at: datetime | None = Field(default=None)  # a quote was recorded
    quote_approved_at: datetime | None = Field(default=None)  # operator approved -> work
    quote_note: str = ""  # vendor/operator quote note
    # Wave E R4 (reject/rework). `rework_round` is 0 on an original dispatch and N on the
    # Nth rework re-dispatch (capped by the `vendor_rework_max_rounds` setting); `rework_of`
    # links a rework leg to the RETURNED leg it supersedes (which is flipped REJECTED).
    # `rework_feedback` is the structured feedback the rework dispatch carried (which segment
    # ids failed which guard lanes / operator notes), stored so the surfaces can render the
    # rework reason. All 0/NULL on an original, never-reworked leg -> byte-identical.
    rework_round: int = Field(default=0)
    rework_of: int | None = Field(default=None, foreign_key="vendordispatch.id")
    rework_feedback: dict | None = Field(default=None, sa_column=Column(JSON))
    # F2 (protocol SNAPSHOT): the transport protocol this leg was DISPATCHED under, captured
    # from vendor_dispatch.vendor_protocol(vendor) at dispatch time. Every protocol-sensitive
    # consumer keys off THIS snapshot (vendor_dispatch.leg_protocol), NOT the vendor's LIVE
    # protocol, so a vendor flipped native<->manual while a leg is parked can never
    # retroactively change how the already-dispatched leg is treated: no phantom manual
    # "delivery" of a native retry through the no-op transport, and no native poll /
    # bearer+claim forwarding for a package a manual leg never put on the wire. NULL on a leg
    # dispatched before migration d4e5f6a7b8c9 added this column -> leg_protocol resolves it to
    # NATIVE (NOT the vendor's live protocol): `manual` did not exist pre-migration, so every
    # such leg was native BY CONSTRUCTION. A live-protocol fallback would resurrect the phantom-
    # delivery / poll-stop bugs on a legacy leg after a native->manual flip.
    protocol: str | None = Field(default=None)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None, foreign_key="customer.id")


# --- Databricks analytical exporter (composable-core phase 2, Train A) --------- #
class DatabricksExportRun(SQLModel, table=True):
    """One export BATCH's audit + resume row (SPEC-databricks-exporter §5.3).

    A NEW TABLE, so a non-alembic SQLite boot gets it from create_all() (app/db.py
    `_ADDED_COLUMNS` is for new COLUMNS on EXISTING tables only) — the exact posture
    documented for the LLMemory tables at models.py:1798-1800; an additive alembic
    migration ships alongside for the Postgres-authority path. JSON columns use
    sa_column=Column(JSON) (the Document.skeleton idiom, models.py:478 — SyncLog is a
    weaker precedent, so the shape is spelled out rather than borrowed, L4).

    The exporter is strictly READ-ONLY against every SOURCE table; the ONLY rows it
    ever writes are these audit rows (+ the one sanctioned once-generate of the
    `databricks_actor_hash_salt` Setting). Nothing here changes a default install:
    the table is empty until the feature is enabled and an export is triggered.
    """

    id: int | None = Field(default=None, primary_key=True)
    # Fresh UUID per batch (invariant I1's run_uuid — immutable, globally unique;
    # the staged artifact dir is keyed on it). Indexed for status lookups.
    run_uuid: str = Field(default="", index=True)
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = Field(default=None)
    # local (stage-only / dry) | databricks (uploaded). A stage-only run is target=local
    # (M5 — dry_run/staged are NOT separate values).
    target: str = Field(default="local", sa_column_kwargs={"server_default": "local"})
    # running | ok | partial | failed. `partial` is a FIRST-CLASS state (M5): per-stream
    # success is possible by design (one stream's load fails while others succeed).
    status: str = Field(default="running", sa_column_kwargs={"server_default": "running"})
    format: str = Field(default="ndjson", sa_column_kwargs={"server_default": "ndjson"})
    # The streams this batch ran, and per-stream bookkeeping (all JSON, nullable):
    streams: list | None = Field(default=None, sa_column=Column(JSON))
    row_counts: dict | None = Field(default=None, sa_column=Column(JSON))
    # per-stream B1/H1/H6 drop counters + the R2-B2 steprun_abandoned_nonterminal /
    # steprun_terminal_no_finished_at buckets.
    dropped_counts: dict | None = Field(default=None, sa_column=Column(JSON))
    # per-append-cursor-stream high-water (resumable; advanced only on confirmed success).
    high_water: dict | None = Field(default=None, sa_column=Column(JSON))
    # per-append-cursor-stream frontier-id-vs-max-id, so a watermark STALL is visible
    # to the operator, not silent (R2-B2).
    watermark_lag: dict | None = Field(default=None, sa_column=Column(JSON))
    # An append-only lakehouse copy cannot see a source row vanish (B1); the run stamps
    # this so operator docs' "erasure does not propagate" statement is machine-visible.
    deletions_not_tracked: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    # which actor-hash salt version keyed this batch's pseudonymization (H3).
    actor_hash_key_version: str = Field(default="", sa_column_kwargs={"server_default": ""})
    error: str | None = Field(default=None)  # redacted (secrets_scrub) before store
    actor: str | None = Field(default=None)  # None = trusted in-process operator
    org_id: int | None = Field(default=None, foreign_key="organization.id")


# --- Comprehensive data-export (full object-graph dump, Batch 13, Train A) ----- #
class DataExportStatus:
    """Lifecycle of one DataExportRun (SPEC-data-export §5.2). Plain strings, like
    every other "enum" in this file.

    QUEUED  — the run row exists; the dispatching Job is not yet terminal.
    RUNNING — the walker is streaming entities into the staged ZIP.
    OK      — every in-scope extractor completed and the artifact is staged.
    PARTIAL — a FIRST-CLASS state (mirrors DatabricksExportRun): one extractor failed
              (e.g. a corrupt JSON column) or the artifact hit data_export_max_bytes;
              its rows land in dropped_counts, the run finishes, the manifest records
              the gap — the whole dump is never aborted for one entity.
    FAILED  — generation failed atomically; NO downloadable artifact is left.
    EXPIRED — the retention reaper removed the staged blob past its TTL (Train C).
    PURGED  — a GDPR erase covering the scope removed the artifact (Train C).
    """

    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    EXPIRED = "expired"
    PURGED = "purged"
    ALL = (QUEUED, RUNNING, OK, PARTIAL, FAILED, EXPIRED, PURGED)
    #: terminal states a completed (downloadable, if not yet reaped) artifact can be in.
    TERMINAL = (OK, PARTIAL, FAILED, EXPIRED, PURGED)


class DataExportRun(SQLModel, table=True):
    """One full-dump export's audit + retention row (SPEC-data-export §5.1).

    Mirrors DatabricksExportRun: a NEW TABLE, so a non-alembic SQLite boot gets it from
    create_all() (app/db.py `_ADDED_COLUMNS` is for new COLUMNS on EXISTING tables only)
    and an additive alembic revision ships alongside for the Postgres-authority path.
    JSON columns use sa_column=Column(JSON) (the DatabricksExportRun idiom).

    The RBAC-confined walker (app/services/data_export.py) is strictly READ-ONLY against
    every source table; the only rows it writes are these audit rows. `error` is
    secrets_scrub'd before store (LAW8). The row is NEVER hard-deleted (LAW8): the
    reaper/GDPR-erase mark it expired/purged and keep it for audit.
    """

    id: int | None = Field(default=None, primary_key=True)
    # Fresh UUID per export; the staged artifact key is derived from it. Indexed for
    # status lookups (the DatabricksExportRun.run_uuid idiom).
    run_uuid: str = Field(default="", index=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # The resolved customer clamp (NULL for a workspace/staff/operator dump).
    customer_id: int | None = Field(default=None)
    actor: str | None = Field(default=None)  # None = trusted in-process operator
    scope_kind: str = Field(default="")  # self | customer | project | workspace
    scope_ref: int | None = Field(default=None)  # customer_id or project_id for the narrowed scopes
    history: str = Field(default="current")  # current | full
    fmt: str = Field(default="jsonl")  # the backbone format (CSV/XLSX are ADDITIONAL emitters)
    emitters: list | None = Field(default=None, sa_column=Column(JSON))  # ["jsonl"] | [...]
    include_audit: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    include_embeddings: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    # include_eval + trusted_operator are PERSISTED (not just carried in the job payload) so the
    # handler re-derives the FULL export decision from this RBAC-written row, never the caller-
    # supplied payload (TA-01: /api/jobs/run lets a caller forge the payload). trusted_operator
    # records whether the submitter was the trusted in-process operator (user=None, host CLI) vs
    # a real API principal, so re-resolution reproduces the SAME confinement without the payload.
    include_eval: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    trusted_operator: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    status: str = Field(
        default=DataExportStatus.QUEUED,
        sa_column_kwargs={"server_default": DataExportStatus.QUEUED},
    )
    job_id: int | None = Field(default=None)  # the dispatching Job (no FK — the JobQueue idiom)
    entity_counts: dict | None = Field(default=None, sa_column=Column(JSON))
    dropped_counts: dict | None = Field(default=None, sa_column=Column(JSON))
    artifact_key: str = Field(default="")  # ExportStore key of the staged ZIP
    artifact_bytes: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    manifest_sha256: str = Field(default="")  # checksum of manifest.json (tamper-evidence)
    chain_verify: str = Field(default="")  # JSON {ok,checked,first_bad_id} when include_audit
    expires_at: datetime | None = Field(default=None)  # retention TTL — the reaper's cutoff
    last_downloaded_at: datetime | None = Field(default=None)  # download-lease grace (DX-16)
    error: str | None = Field(default=None)  # secrets_scrub'd before store
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = Field(default=None)


# ============================================================================== #
# ADVANCED WORKFLOWS -- the LLM Localization Harness (SPEC-advanced-workflows, P0).
#
# A SECOND workflow tier that lives entirely BESIDE the classic step-list engine.
# The HARD RULE (spec §2): the classic engine (Workflow / WorkflowStep / WorkflowRun /
# StepRun and their orchestrator) is UNTOUCHED. These are NEW tables in a NEW namespace;
# nothing here is an edit of a classic model. A mission is a durable, reusable config; a
# run is one execution of a mission through the deterministic harness loop; the transcript
# is the durable, auditable record of every decision; a staged target is a candidate
# translation the harness produced WITHOUT ever writing a classic content table (P0 delivery
# is a stub -- P1 wires real delivery). All four tables are made by create_all() on a fresh
# SQLite/test boot (new tables, not altered ones) and by the alembic revision
# c9f2a7b4e8d1 on the Postgres-authority path (the DataExportRun idiom); the nullability-
# parity CI guard (tests/verify_migrations.py) pins this revision's columns to the models.
# ============================================================================== #
class AdvancedRunStatus:
    """Lifecycle of one AdvancedRun (SPEC-advanced-workflows §3.5). Plain strings, like
    every other "enum" in this file, so `status` is an unconstrained TEXT column (no
    migration to add a value).

    PENDING       -- the run row exists; the harness loop has not started.
    RUNNING       -- the loop is executing tool requests.
    PAUSED        -- the loop stopped on a durable, resumable boundary (P0 does not
                     auto-resume; a run parks here rather than silently restarting spend).
    COMPLETED     -- the scripted mission reached `deliver` (or ran its actions out) cleanly.
    FAILED        -- the loop failed for a reason that is not a budget stop (a fence trip,
                     a tool refusal, an internal error) with a partial transcript persisted.
    FAILED_BUDGET -- a live per-run USD/token cap tripped in the dispatcher before a spend
                     tool; the run stops with its partial transcript (a distinct, honest
                     status from FAILED so the timeline reads "stopped on budget", not "broke").
    CANCELLED     -- a cooperative cancel flag, polled between tool calls, stopped the run
                     (a REAL cancellation for this tier, unlike the classic cancel_run which
                     only flips DB status while a thread runs on).
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_BUDGET = "failed_budget"
    CANCELLED = "cancelled"
    ALL = (PENDING, RUNNING, PAUSED, COMPLETED, FAILED, FAILED_BUDGET, CANCELLED)
    #: terminal states -- a run in one of these is finished and non-resumable.
    TERMINAL = (COMPLETED, FAILED, FAILED_BUDGET, CANCELLED)


class AdvancedTranscriptKind:
    """The kinds of durable transcript entry (SPEC-advanced-workflows §3.6). Plain strings."""

    PLAN = "plan"  # the planner's requested action (what the model/script asked for)
    TOOL_CALL = "tool_call"  # a dispatched tool call (tool + args + tier)
    OBSERVATION = "observation"  # a tool result digest appended to the transcript
    DELEGATE_RESULT = "delegate_result"  # a bounded provider engagement's result
    ADJUDICATION = "adjudication"  # a discrepancy reconciliation with rationale
    DELIVERY_MANIFEST = "delivery_manifest"  # the terminal deliver record
    NOTE = "note"  # a harness note (a fence trip, a budget stop, a cancel)
    # SPEC-quality-lenses-p2 §5.5 (T3): a quality.judge verdict for one unit -- the lens slug,
    # the computed score, passed/blocked, the blocking check ids, and per-finding severity +
    # quoted span text (a SELF-DESCRIBING payload, L6, never bare integers).
    QUALITY = "quality"
    ALL = (
        PLAN,
        TOOL_CALL,
        OBSERVATION,
        DELEGATE_RESULT,
        ADJUDICATION,
        DELIVERY_MANIFEST,
        NOTE,
        QUALITY,
    )


class AdvancedMission(SQLModel, table=True):
    """A reusable Advanced-Workflow mission: a brief, a tool allowlist, a model roster, a
    budget, fences, and a project/customer binding (SPEC-advanced-workflows §2/§3). The
    Conductor (P1) or the P0 ScriptedPlanner owns a RUN inside these fences; the mission is
    the durable config, never the execution.

    org_id/customer_id are resolved from the bound project at create time (the root-owned-row
    idiom) so the dispatcher can pass the run's ring EXPLICITLY into every tool call -- the
    delete_workflow-train lesson (confinement-by-import is the bug class) applied with force.
    JSON columns use sa_column=Column(JSON, nullable=False) (the DataExportRun idiom). The
    budget/fence scalars are the mission's OWN copy (seeded from the install defaults at
    create time), so re-tuning the install defaults never retroactively re-fences a mission."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None)
    project_id: int = Field(foreign_key="project.id")
    name: str = ""
    # SPEC-quality-lenses-p2 §5.1 (T3): the Quality Lens that gates this mission's deliveries.
    # NULL = NO lens = today's behavior (P2-D14): a pre-T3 mission (whose tool_allowlist snapshot
    # lacks quality.judge) runs UNGATED, unchanged. `create_mission` stamps the install default
    # lens id EXPLICITLY at create time, so NULL is only ever a pre-T3 row or an explicit "no lens".
    # No FK (the customer_id idiom): a lens may be forked/re-homed without an FK blocking the write.
    lens_id: int | None = Field(default=None)
    brief: str = ""  # what to localize, into what, to what bar (free text; P0 is descriptive)
    # The fail-closed tool ALLOWLIST for this mission: a JSON list of tool names the harness
    # dispatcher will admit (a tool not here is refused even if it is in the global registry).
    tool_allowlist: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # The provider/model roster the mission may use (P0 minimal: {"provider": "...",
    # "model": "..."}); the harness reads it when a spend tool needs a target engine.
    model_roster: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # The P0 ScriptedPlanner action list: a deterministic JSON sequence of tool requests that
    # PROVES the rails without a model. P1's Conductor ignores this and plans live.
    scripted_actions: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # The mission's own budget + fences (seeded from the install defaults at create).
    budget_usd: float = 0.0  # per-run USD ceiling; 0 = unlimited (CostBudget convention)
    max_tokens: int = 0  # per-run token ceiling; 0 = unlimited
    max_tool_calls: int = 50  # fence: max dispatcher tool calls per run
    # SPEC-quality-lenses-p2 §5.3 / P2-D15: raised 6 -> 8. Today's committee spends up to 5 per
    # unit (describe_image + up to 2 text delegates + a vision-direct translate + one adjudication)
    # and `quality.judge` adds a 6th; 6 fit with ZERO headroom, so the default is 8 to leave margin.
    max_provider_calls_per_unit: int = 8  # fence: max spend calls per work unit
    wall_clock_seconds: int = 900  # fence: wall-clock cap for the whole run (0 = unlimited)
    # Advanced fix bundle round 1 (M2): the fence values the CALLER EXPLICITLY requested at
    # create time, as a JSON object ({axis: requested}). The install ceiling may then have
    # reduced them; mission_view re-derives the "requested N, capped at M" clamp notices from
    # this + the current install maxes on EVERY read, so the clamp signal is durable across all
    # four surfaces + the run's plan transcript entry (not just the one-shot create response).
    # {} = nothing explicit was requested (a defaulted mission) -> no clamp notices.
    requested_fences: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_by: str = ""  # email/pseudo-actor that authored the mission (attribution)
    created_at: datetime = Field(default_factory=_utcnow)


class AdvancedRun(SQLModel, table=True):
    """One execution of an AdvancedMission through the deterministic harness loop. Carries
    the live spend/fence actuals, the cooperative cancel flag (a REAL cancel for this tier),
    and the LLM Ledger correlation ids, so the transcript rows and the ledger rows join."""

    id: int | None = Field(default=None, primary_key=True)
    mission_id: int = Field(foreign_key="advancedmission.id")
    # Resolved from the mission at launch (the ring the dispatcher passes into every tool).
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None)
    project_id: int = Field(foreign_key="project.id")
    status: str = AdvancedRunStatus.PENDING
    # The cooperative cancel flag: set by cancel (web/CLI/API/MCP), POLLED by the loop between
    # tool calls. A True here makes the next inter-call poll stop the run CANCELLED -- unlike
    # the classic cancel_run, this actually halts the in-flight work at the next boundary.
    cancel_requested: bool = False
    # LLM Ledger correlation (the orchestrator's tap_envelope idiom): one action_id/run
    # correlation minted at launch and stamped into every provider call's tap_meta, so the
    # ledger rows for this run all carry the same envelope and roll up by run.
    action_id: str | None = None
    correlation_id: str | None = None
    # Live actuals (updated as the loop runs; the transcript carries the per-entry detail).
    spent_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0  # dispatcher tool calls made so far (fence + observability)
    provider_calls: int = 0  # spend-tool provider calls made so far
    notes: str | None = None  # why the run ended the way it did (fence/budget/cancel/error)
    created_by: str = ""  # email/pseudo-actor that launched the run (attribution)
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None  # terminal authority (NULL while not terminal)
    # ORPHAN RECOVERY (BACKLOG 2026-09-01 "orphaned run recovery gap"): a liveness stamp the
    # harness loop touches at every iteration + after every tool call, so a periodic sweep can
    # tell a live RUNNING run (fresh heartbeat) from a dead one whose host process died mid-loop
    # (no finally ran, so it is stuck RUNNING forever). NULL on a pre-sweep row = fall back to
    # started_at for staleness (a legacy row created before this column existed). The sweep marks
    # a stale RUNNING run FAILED with notes="stop: orphaned" and abandons its staged targets.
    heartbeat_at: datetime | None = None


class AdvancedTranscriptEntry(SQLModel, table=True):
    """One durable transcript row (SPEC-advanced-workflows §3.6): the plan, every tool call +
    observation digest, every delegate result, every adjudication, and the final delivery
    manifest -- the product inside the product (the debugging surface, the audit story, the
    marketing demo). Appended write-coalesced by the harness (the SQLite single-writer
    reality: the loop batches appends rather than writing per tool call to a hot table).

    `payload` is the entry's structured digest as a JSON object; `cost_usd` is the spend this
    entry billed (0 for a read/plan/note). Rows are correlated to LLM Ledger rows through the
    run's action_id/correlation_id, so "show me why the agent chose Reservar" joins cleanly."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="advancedrun.id")
    seq: int = 0  # monotonic order within the run (0-based; the transcript's sort key)
    kind: str = ""  # one of AdvancedTranscriptKind
    tool: str | None = None  # the tool name for a tool_call/observation entry, else NULL
    tier: str | None = None  # the tool's tier (read/write/spend) for a tool_call, else NULL
    cost_usd: float = 0.0  # USD this entry billed (0 for read/plan/note)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=_utcnow)


class AdvancedStagedTarget(SQLModel, table=True):
    """A candidate target the harness STAGED for a unit (SPEC-advanced-workflows §3.2
    `target.propose`). It lives in the run's OWN table and NEVER touches a classic content
    table (Segment / StringKey / Document): P0 `deliver` only finalizes the transcript and
    marks the run delivered -- P1 wires real delivery through the existing delivery seams. This
    is the write-staged tier made concrete: a model can propose, but nothing it proposes
    reaches production content in P0."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="advancedrun.id")
    unit_key: str = ""  # the segment/key identity within the mission's project scope
    # P3 T2 (SPEC-advanced-workflows §8): a DOCUMENT unit's real identity is its Segment id, a
    # PLAIN nullable indexed integer with NO database ForeignKey (a SOFT reference, ring-validated
    # in code and bound to unit_key = f"seg:{segment_id}" at stage time). A hard FK would raise
    # IntegrityError in the three classic segment-delete sites (retention.delete_document,
    # content.delete_project, the locale-removal Segment delete) and would put the run's staged
    # table inside a content cascade the HARD RULE forbids editing -- unit_key already references a
    # StringKey by NAME with no FK for exactly this reason. NULL = a keys unit (or a pre-T2 row).
    segment_id: int | None = Field(default=None, index=True)
    # P3 T2 (round-2 M2): sha256 of the source string the DELEGATE translated (captured at stage
    # time from the body the drive-time seam read, NEVER a fresh Segment read). `_deliver`'s
    # document branch re-checks it against the CURRENT segment, catching a recycled Segment.id
    # (SQLite reuses max(rowid)+1 after a delete-then-import) and a human edit landing between drive
    # and deliver. Mirrors the SegmentTM.source_hash / SegmentEmbedding.source_hash fingerprint
    # pattern. NULL = a pre-T2 row; a keys row carries an INERT hash its delivery never consults.
    source_hash: str | None = Field(default=None)
    # P3 T3 (SPEC §8 FULL_TEXT): a FULL_TEXT paragraph unit's 0-based POSITION in
    # Document.skeleton["units"] -- the identity fact bound at stage time (with segment_id) and
    # re-checked at deliver, the positional reassembly order, AND the `_deliver` dispatch signal
    # (non-NULL <=> a FULL_TEXT paragraph row). A plain nullable integer, NO ForeignKey and NO
    # index (delivery groups all of a run's rows in memory by run_id; nothing queries by it).
    # NON-NULL binds a FULL_TEXT paragraph unit (segment_id, paragraph_index); NULL = a keys /
    # SEGMENTED document / pre-T3 row (byte-identical).
    paragraph_index: int | None = Field(default=None)
    # The RESOLVED StringKey id for this unit (BACKLOG 2026-09-03 "deliver ignores key_ids /
    # namespace"). `target.propose` stamps the unit's key id (ring-validated through the
    # dispatcher's key_ids validator to the run's project) so `deliver` resolves the exact key by
    # id instead of re-resolving unit_key by NAME in the DEFAULT namespace -- which silently
    # skipped every unit of a namespaced keys project (`keys import --namespace moz`) with "no
    # such key in project". NULL = no id was carried (a scripted mission, or a model-only unit);
    # `deliver` then falls back to a `namespace::name` parse of unit_key. No FK (the customer_id
    # idiom): a key may be re-homed without an FK blocking the staged write.
    key_id: int | None = Field(default=None)
    # Advanced fix bundle round 1 (H1/M3): the unit's RECORDED namespace, stamped by
    # target.propose from the authored unit config. `deliver` resolves the exact NAME
    # (`unit_key`) in THIS namespace first, then the default namespace -- NEVER splitting the
    # name on `::` (a nested-JSON key name contains `::` too, so splitting silently mis-read
    # `errors::generic` as namespace `errors`). "" = the default namespace (byte-identical to a
    # pre-fix row). No FK (namespace is a string identity segment, not a row id).
    namespace: str = ""
    source_text: str = ""
    target_text: str = ""
    provider_used: str | None = None
    model_used: str | None = None
    # P1 provenance, STRUCTURED (never packed into model_used, which holds a model id): whether
    # the unit's screenshot was actually read for this target -- "read" (a description was used),
    # "none" (image.fetch ran and the key has no image), "unavailable" (we could not determine
    # whether there is an image, or could not read it), or "n/a" (P3 T2: no image PATH exists for
    # this unit, e.g. a document unit -- distinct from "none", which means image.fetch ran and
    # found nothing) -- and whether a second opinion settled it. Both ride into the delivery
    # manifest, so a delivered target says what it was based on.
    vision: str | None = None
    adjudicated: bool = False
    # SPEC-quality-lenses-p2 §5.2 (T3, H7): stage THEN gate. `target.propose` writes the row;
    # `quality.judge` stamps the evaluation id + the blocked flag onto it; `deliver` SKIPS a
    # flagged row and names the blocking check(s) in the manifest. NULL evaluation id = this row
    # was never judged (a pre-T3 / ungated mission), `quality_blocked` False = judged clean OR
    # never judged. A stamped row is the durable record of why a unit did not ship.
    quality_evaluation_id: str | None = None
    quality_blocked: bool = False
    # ORPHAN RECOVERY (BACKLOG 2026-09-01): the orphaned-run sweep sets this True on every staged
    # row of a run it marks FAILED "stop: orphaned", so a dead run's proposals can NEVER deliver
    # even if a deliver pass were somehow attempted against them. `deliver` skips an abandoned row
    # (defence in depth); False on every live row = byte-identical to today.
    abandoned: bool = False
    # round-3 L2: the instant a document/key erasure SEVERED this row's content
    # (reap_document_content arm (c) NULLs source/target/hash/segment_id/key_id). A severed keys
    # row would otherwise resolve AGAIN by a namespace::name lookup after a delete+restore and
    # deliver an EMPTY target (the lens `empty` check ran BEFORE the erasure), so `deliver` skips
    # any row with this stamp. NULL = never erased (byte-identical to a pre-fix row); the DOCUMENT
    # arm was already fail-closed by its segment_id recheck, this makes the KEYS arm fail-closed
    # too. A timestamp (not a bool) so it needs NO renderer change -- nothing renders it. No FK,
    # no index (deliver groups a run's rows in memory).
    erased_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


# --- Quality Lenses (SPEC-quality-lenses-p2 section 2) ------------------------
# The P2 substrate: a judge emits FINDINGS (span + severity), and a named, configurable
# LENS turns those findings into a deterministic number. Five new tables, all additive and
# empty on upgrade; no column is added to any existing table by T1, so `_ADDED_COLUMNS`
# needs no twin here (the mission/staged-target columns land with T3).
class QualityLensKind:
    """Which scorer AND which prompt contract a lens selects (spec section 2.1 `kind`).

    JUDGE            - an LLM judge emits findings; the score is 100 minus the weighted
                       penalty sum (`mqm`, `esa`).
    CHECKLIST        - its own prompt and parser: one boolean result per assertion; the
                       score is the pass fraction (spec section 3.4, M6).
    POST_EDIT_EFFORT - NO judge call at all: the score is derived from the post-edit
                       distance (spec section 3.4, M5).
    """

    JUDGE = "judge"
    CHECKLIST = "checklist"
    POST_EDIT_EFFORT = "post_edit_effort"
    ALL = (JUDGE, CHECKLIST, POST_EDIT_EFFORT)


class QualityFindingOrigin:
    """Who authored a finding. `judge` = the LLM verdict, `check` = a deterministic check
    from app/services/checks.py, `human` = a reviewer (reserved; P2 writes none)."""

    JUDGE = "judge"
    CHECK = "check"
    HUMAN = "human"
    ALL = (JUDGE, CHECK, HUMAN)


class QualityFindingKind:
    """SPAN = the finding points at a substring of the TARGET. OMISSION = something in the
    SOURCE was left out, so there is nothing in the target to point at and the finding is
    anchored to a SOURCE span instead (spec H3). Rejecting an omission for lacking a target
    span would make the whole error class unreportable."""

    SPAN = "span"
    OMISSION = "omission"
    ALL = (SPAN, OMISSION)


class QualityLens(SQLModel, table=True):
    """A named, configurable quality lens: the taxonomy, the severity vocabulary, the weight
    table and the bar that turn a judge's findings into a number (spec section 2.1).

    A BUILTIN is read-only: an edit FORKS to a customer-owned copy (the EvalCandidate
    immutability idiom above), and builtins are seeded PER ORG at first use rather than once
    globally (L5), so one org's fork never re-points another's lens.

    `slug` is unique per (org_id, customer_id). SQLite cannot express that as a UNIQUE index
    without a functional coalesce() index (NULLs never collide), so the rule lives in the
    single writer (`quality_lens.ensure_builtins` / the T3 CRUD spine) and is asserted in the
    suite -- the same posture section 2.3 takes for the exactly-one-parent rule, and an
    unenforceable constraint comment would be a fiction.

    `threshold` is NEVER rendered into a judge prompt (parent spec section 6.4, anchoring)."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    # No FK, the AdvancedMission/LQAResult idiom: a lens may be scoped to a customer id that
    # a later ring change re-homes without an FK blocking the write.
    customer_id: int | None = Field(default=None)
    slug: str = Field(default="", index=True)  # 'mqm' | 'esa' | 'checklist' | 'post_edit_effort'
    title: str = ""  # display name ("Post-edit effort")
    builtin: bool = False
    kind: str = QualityLensKind.JUDGE  # one of QualityLensKind
    # [] means NO taxonomy (esa): the judge is told not to categorize, and a category it
    # emits anyway is STRIPPED rather than rejected (spec section 3.2 step 7).
    dimensions: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    severities: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # The deterministic weight table AND the source of rank() (H4): rank derives from the
    # WEIGHT, never from the position of a severity in `severities`, so a lens that reweights
    # its vocabulary keeps a consistent block ordering.
    weights: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    block_severity: str = "major"
    threshold: int = 90
    max_findings: int = 5  # the per-segment parser cap; exceeding it rejects the verdict
    gate_policy: str = "block"  # 'block' | 'flag'
    checks: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # The row-3 per-lens opt-in: check ids that BLOCK for this lens even though they are
    # advisory by default. Only `length` may be opted in (char_limit overflow); approved-term
    # absence is advisory ONLY, on the record (row 5), and the writer refuses it here.
    blocking_checks: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    adjudication: bool = False  # default OFF for a no-taxonomy lens (P2-D13)
    normalize_per_words: int = 0  # RESERVED: any non-zero value is refused at write (M7)
    assertions: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    archived_at: datetime | None = None  # soft archive, never a hard delete (L4)
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class QualityLensExample(SQLModel, table=True):
    """A curated few-shot SPAN example for a lens (spec section 2.2, P2-D6).

    Spans only: a payload carrying a score/rating/quality value is refused at write time,
    because a scored example ANCHORS the judge on a number (parent spec section 6.4) and the
    whole point of P2 is that the judge stops producing numbers. At most 4 rows are selected
    per (lens, customer, locale pair) at prompt-build time, ordered by id."""

    id: int | None = Field(default=None, primary_key=True)
    lens_id: int = Field(foreign_key="qualitylens.id", index=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None)
    source_locale: str = ""
    target_locale: str = ""
    source_text: str = ""
    target_text: str = ""
    span_start: int | None = None
    span_end: int | None = None
    severity: str = ""
    category: str | None = None
    note: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class QualityFinding(SQLModel, table=True):
    """ONE finding: a span (or an omission) with a severity, under a named lens (spec 2.3).

    THE SCORE IS NOT REPEATED HERE. `evaluation_id` groups the findings of one evaluation
    (mirroring LQAResult.scoring_id); the score and the pass/fail live on the transcript entry
    and the staged target, so a findings query can never disagree with the card about the
    number.

    EXACTLY-ONE-PARENT, WRITER-ENFORCED (M15): precisely one of `run_id`, `segment_id`,
    `document_id` is non-NULL on any row. SQLite cannot express that as a constraint, so the
    rule lives in the single writer (`quality_lens.record_findings`) and is asserted in the
    suite; an unenforced CHECK comment would be a fiction.

    `org_id`/`customer_id` are DENORMALIZED (M15), stamped by the writer from the run/segment
    ring, so a findings query never needs a three-table join to establish confinement.
    `lens_slug` is denormalized for the same class of reason: a lens can be forked or edited
    after the fact, and a finding must always name what produced it.

    Relation to LQAResult: NONE in P2, and no backfill (P2-D7). LQAResult stays the classic
    tier's scored-card row; inventing span offsets for legacy rows would be fabricated
    provenance."""

    id: int | None = Field(default=None, primary_key=True)
    evaluation_id: str = Field(default="", index=True)  # uuid4 hex: ONE evaluation's findings
    org_id: int | None = Field(default=None, foreign_key="organization.id")
    customer_id: int | None = Field(default=None)
    # Exactly ONE of the three is non-NULL (writer-enforced, above).
    run_id: int | None = Field(default=None, foreign_key="advancedrun.id")
    segment_id: int | None = Field(default=None, foreign_key="segment.id")
    document_id: int | None = Field(default=None, foreign_key="document.id")
    staged_target_id: int | None = Field(default=None, foreign_key="advancedstagedtarget.id")
    unit_key: str = ""  # the key/segment identity inside the run's project
    lens_id: int | None = Field(default=None, foreign_key="qualitylens.id")
    lens_slug: str = ""
    origin: str = QualityFindingOrigin.JUDGE
    kind: str = QualityFindingKind.SPAN
    check_id: str | None = None  # 'placeholders' | 'tags' | ... for an origin='check' row
    severity: str = ""  # canonical, lowercased, in the lens vocabulary
    category: str | None = None  # NULL for a no-taxonomy lens
    # Target offsets; NULL/NULL/"" for an omission, which has nothing in the target to point at.
    span_start: int | None = None
    span_end: int | None = None
    span_text: str = ""
    # Source offsets; SET for an omission (H3).
    source_start: int | None = None
    source_end: int | None = None
    source_span_text: str = ""
    note: str = ""
    engine_ref: str | None = None  # the judge
    engine_model: str | None = None
    engine_family: str | None = None
    translator_ref: str | None = None  # the pairing, on EVERY finding (spec section 7)
    translator_model: str | None = None
    translator_family: str | None = None
    # True / False / None-is-UNKNOWN -- the three-state honesty the adjudicator already ships.
    # An unresolvable family stays None and renders as "unknown", NEVER as separation.
    family_separation: bool | None = None
    source_locale: str = ""
    target_locale: str = ""
    content_type_id: int | None = None
    adjudication_outcome: str | None = None  # 'upheld' | 'withheld' | NULL
    # The MQM-APE hook: P2 DEFINES this column and leaves it NULL; P3 writes it.
    post_edit_fate: str | None = None  # 'fixed' | 'unchanged' | NULL
    created_at: datetime = Field(default_factory=_utcnow)


class QualityGoldItem(SQLModel, table=True):
    """A human-labelled gold item for the meta-evaluation harness (spec section 2.4, H9).

    WHY A STORED HYPOTHESIS AND NOT `EvalDatasetItem.reference_text`: reference_text is
    MUTABLE by design -- the Lab's documented escape hatch is to promote a candidate's output
    (or a human edit of it) to BE the reference, which is just an UPDATE of that column. Gold
    labels attached to it would be silently invalidated by a promotion, with no error.
    `hypothesis_text` is the exact string the labeller judged, `hypothesis_sha` pins it, and
    `locked` refuses edits after the first label. The meta-eval (T2) judges THAT text and
    nothing else, and recomputes the sha before reporting any metric.

    SEPARATE FROM `QualityFinding`, not `origin='gold'` rows on it: the Lab's isolation rule
    is structural (a Lab run never writes production tables), so keeping gold out of the
    production findings table means no production report can average it in."""

    id: int | None = Field(default=None, primary_key=True)
    dataset_item_id: int = Field(foreign_key="evaldatasetitem.id", index=True)
    hypothesis_text: str = ""  # IMMUTABLE once labelled (`locked`)
    hypothesis_sha: str = ""
    gold_score: int | None = None  # 0..100, NULL = no scalar gold for this item
    labeler: str = ""
    locked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class QualityGoldLabel(SQLModel, table=True):
    """One human span (or omission) label on a gold item (spec section 2.4).

    Overlap against a judge finding is computed PER KIND by the meta-eval (T2, N-M3): a span
    label's TARGET range against a span finding's target range, an omission label's SOURCE
    range against an omission finding's source range, and cross-kind pairs never overlap --
    comparing a span's target offsets against an omission's source offsets is comparing
    indices into two different strings."""

    id: int | None = Field(default=None, primary_key=True)
    gold_item_id: int = Field(foreign_key="qualitygolditem.id", index=True)
    kind: str = QualityFindingKind.SPAN  # 'span' | 'omission'
    span_start: int | None = None
    span_end: int | None = None
    span_text: str = ""
    source_start: int | None = None
    source_end: int | None = None
    severity: str = ""
    category: str | None = None
    note: str = ""
    labeler: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class AssistAllowance(SQLModel, table=True):
    """Durable per-identity Ask/Draft allowance, shared across all assist doors."""

    actor_hash: str = Field(primary_key=True)
    day: str = ""
    daily_calls: int = 0
    minute_started: float = 0.0
    minute_calls: int = 0


class BlobDeleteOutbox(SQLModel, table=True):
    """Durable retry ledger for blob deletions that FAILED during a document erase (#399).

    When ``retention.delete_document`` erases a document and the blob delete reports
    ``DeleteStatus.FAILED`` (a real operational refusal -- a Windows open handle, a
    permission error, a transient S3 HEAD/delete error -- NOT a missing blob), the blob's
    canonical key is persisted HERE in the SAME transaction as the row deletes. Without
    that record the erased document is the key's last durable reference: the next sweep
    has no document to retry and the uploaded content outlives its database identity
    forever (finding dbdata-07). ``retention.purge`` drains the ledger every sweep: a
    retry that REMOVES the blob (or proves it MISSING) deletes the row; a retry that
    FAILS again bumps ``attempts`` and keeps the key until it succeeds.

    ``source_document_id`` is a VALUE, not a foreign key: the Document row it names is
    deleted in this very transaction -- an FK would either block the erase (Art. 17 must
    never be refused because storage is flaky) or dangle by design. It exists only so a
    log/forensics reader can name the document that produced the key.

    A NEW TABLE, so a non-alembic SQLite boot gets it from create_all() and the additive
    alembic revision ships alongside for the Postgres-authority path (the DataExportRun
    idiom); the column-completeness guard in tests/verify_migrations.py pins the
    revision's column set to this model. Classified EXPLICIT_EXCLUSIONS in
    data_export_part2: the ledger's purpose is erasure, it carries no portable content."""

    id: int | None = Field(default=None, primary_key=True)
    # Exact blob identity (UUID for new uploads, id.format for legacy), unique so retries can
    # never stack duplicate pending rows for one key.
    blob_key: str = Field(default="", index=True, unique=True)
    source_document_id: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    created_at: datetime = Field(default_factory=_utcnow)


class NotifyChannelKind:
    """The channel adapters (app/services/notify.py): where a project event is posted."""

    SLACK = "slack"
    TEAMS = "teams"
    WEBHOOK = "webhook"
    EMAIL = "email"
    ALL = (SLACK, TEAMS, WEBHOOK, EMAIL)


class NotifyChannel(SQLModel, table=True):
    """An org-owned outbound notification destination (Slack, Teams, signed webhook, email).

    `secret_enc` is a Fernet blob (the Vendor / Connection pattern) holding the destination URL
    (an incoming-webhook URL is a bearer credential) and, for the generic webhook, the HMAC
    signing secret. It is write-only: no surface ever returns it, only a masked tail of the URL.
    `config` holds the non-secret settings (email recipients). `customer_id` optionally pins the
    channel to one customer (a customer-bound caller only sees its own customer's channels).

    A NEW TABLE (create_all on SQLite, one additive alembic revision otherwise); classified
    EXPLICIT_EXCLUSIONS in data_export_part2 (credential material, no portable content)."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="organization.id", index=True)
    customer_id: int | None = Field(default=None, foreign_key="customer.id")
    kind: str = Field(default=NotifyChannelKind.SLACK)
    name: str = ""
    config: dict | None = Field(default=None, sa_column=Column(JSON))
    secret_enc: str = ""
    enabled: bool = Field(default=True, sa_column_kwargs={"server_default": "1"})
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_message: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class NotifyRoute(SQLModel, table=True):
    """Which channel a scope posts to. `scope` is project | content_type | project_type |
    customer | org; `scope_ref` is the id (or the project type name; empty for org). Resolution
    is most specific first (project, content type, project type, customer, org default).
    `events` empty/NULL = the default-on events. A row with `channel_id` NULL is a MUTE: the
    scope (in practice one project) posts nowhere."""

    __table_args__ = (UniqueConstraint("channel_id", "scope", "scope_ref", name="ux_notifyroute"),)
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, index=True)
    channel_id: int | None = Field(default=None, foreign_key="notifychannel.id", index=True)
    scope: str = "org"
    scope_ref: str = ""
    events: list | None = Field(default=None, sa_column=Column(JSON))
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class NotifyOutbox(SQLModel, table=True):
    """One delivery of one event to one channel: the outbox AND the delivery log. Written in the
    caller's transaction (no lost or phantom events), delivered right after commit, retried by
    the scheduler tick (1 min, 5 min, 30 min, then `failed`). `payload` holds the neutral facts
    (names, counts, status link): NEVER content text. `(channel_id, dedup_key)` is unique, so a
    replayed event is one message."""

    __table_args__ = (UniqueConstraint("channel_id", "dedup_key", name="ux_notifyoutbox_dedup"),)
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, index=True)
    channel_id: int = Field(foreign_key="notifychannel.id", index=True)
    project_id: int | None = Field(default=None, index=True)
    event: str = ""
    dedup_key: str = ""
    payload: dict | None = Field(default=None, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    next_attempt_at: datetime | None = None
    last_error: str = ""
    http_status: int | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    sent_at: datetime | None = None
