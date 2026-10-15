"""Vendor Studio data model (SQLModel/SQLite). Own schema, own DB — no shared tables.

The status ladder (VProjectStatus) mirrors PROPOSAL §4:
  NEW -> CLAIMED -> IN_PROGRESS -> IN_QA -> COMPLETED -> DELIVERED  (+ CANCELLED)

Segment statuses (VSegStatus) mirror the CAT research (MateCat/memoQ synthesis):
  NEW / PREFILLED / EDITED / CONFIRMED / APPROVED  (+ REJECTED back-state via issue_tag)
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import JSON, Column, Field, SQLModel


# --- enums (plain string constants; SQLite stores them as TEXT) --------------
class VProjectStatus:
    NEW = "NEW"
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    IN_QA = "IN_QA"
    COMPLETED = "COMPLETED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"

    #: Order for forward-only ladder validation (CANCELLED is terminal, off-ladder).
    LADDER = [NEW, CLAIMED, IN_PROGRESS, IN_QA, COMPLETED, DELIVERED]  # noqa: RUF012


class VSegStatus:
    NEW = "NEW"
    PREFILLED = "PREFILLED"
    EDITED = "EDITED"
    CONFIRMED = "CONFIRMED"
    APPROVED = "APPROVED"


class Provenance:
    TM_EXACT = "tm_exact"
    TM_FUZZY = "tm_fuzzy"
    MT = "mt"
    NEW = "new"


# --- tables ------------------------------------------------------------------
class VProject(SQLModel, table=True):
    """One received dispatch leg = one Vendor Studio project."""

    __tablename__ = "vproject"

    id: int | None = Field(default=None, primary_key=True)
    contract_version: str = ""
    # The Localization OS run id / step, kept as OPAQUE strings (we never interpret them).
    run_ref: str = ""
    step_key: str = ""
    step_name: str = ""
    # The claim token is a BEARER CREDENTIAL for the return. Stored so we can deliver,
    # NEVER rendered in any template or API response.
    claim_token: str = Field(default="", index=True)
    customer_label: str = ""
    source_locale: str = ""
    target_locale: str = ""
    status: str = Field(default=VProjectStatus.NEW, index=True)
    expiry_at: datetime | None = None
    # The TM-band estimate table (opaque JSON as dispatched).
    estimate: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # Package-level glossary block (assets.glossary), when present.
    glossary: list = Field(default_factory=list, sa_column=Column(JSON))
    style_guide: str = ""
    context: str = ""
    # Assignment (personas, not accounts).
    claimed_by: str = ""
    assigned_to: str = ""
    # Cost reconciliation: prefilled from estimate, editable before deliver.
    actual_cost_usd: float | None = None
    actual_currency: str = "USD"
    cancel_reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    delivered_at: datetime | None = None
    # Cumulative set of contract ids already delivered (for partial tranches). JSON list.
    delivered_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    # L4: a stable fingerprint of the dispatched payload (ids + sources + locale). A
    # re-POST of the SAME claim token with a DIFFERENT payload is a conflict, not an
    # idempotent retry.
    dispatch_fingerprint: str = ""
    # Vendor Wave E — QUOTE GATE (R3). A quote-gated dispatch carries top-level
    # "quote_requested": true; the PM must submit a quote BEFORE starting work, and Loc
    # Studio then approves/declines it out of band.
    #   quote_status: "" (not gated) | "requested" | "submitted" | "approved" | "declined"
    quote_requested: bool = False
    quoted_cost_usd: float | None = None
    quote_status: str = ""
    # Vendor Wave E — REWORK (R4). A rework arrives as a NORMAL signed dispatch with a
    # FRESH claim token, carrying a top-level "rework" block. We store the block verbatim
    # and flag the project so the inbox/editor render the rework context.
    is_rework: bool = False
    rework: dict | None = Field(default=None, sa_column=Column(JSON))


class VSegment(SQLModel, table=True):
    __tablename__ = "vsegment"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="vproject.id")
    # The dispatch contract's segment id (opaque string; "12" or "full:0").
    contract_id: str = Field(default="", index=True)
    order_index: int = 0
    source: str = ""
    # The current editable target.
    target: str = ""
    # The exact target as dispatched (prefill) — the diff baseline + unedited-MT check.
    prefill: str = ""
    dispatch_state: str = ""
    status: str = Field(default=VSegStatus.NEW)
    provenance: str = ""  # tm_exact|tm_fuzzy|mt|new|"" (unknown)
    match_score: float | None = None
    locked: bool = False
    locked_reason: str = ""
    placeholders: list = Field(default_factory=list, sa_column=Column(JSON))
    char_limit: int | None = None
    # Reviewer reject tag; set when a reviewer rejects (segment routes back to EDITED).
    issue_tag: str = ""
    # Vendor Wave E — REWORK (R4). Per-segment buyer feedback carried on a rework dispatch:
    # {"issues": ["tag_damage", "glossary_violation", ...], "note": "<str>"}. Rendered in the
    # editor next to the reject tag / comments so the translator sees exactly what to fix.
    feedback: dict | None = Field(default=None, sa_column=Column(JSON))
    confirmed_by: str = ""
    confirmed_at: datetime | None = None


class VComment(SQLModel, table=True):
    __tablename__ = "vcomment"

    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(index=True, foreign_key="vsegment.id")
    author: str = ""
    role: str = ""
    body: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Vendor Wave 3 (Train E, §7.5) — the buyer round trip.
    #   direction: 'in'   = a buyer query carried in on the dispatch package (read-only,
    #                       rendered above the local list, marked as from the buyer);
    #              'local' = a persona's own comment (the default);
    #              'out'   = reserved.
    #   external_ref: for an 'in' comment, the BUYER's thread id; for a 'local' reply that
    #                 targets a buyer thread, the same id -> becomes the return's `reply_to`.
    #   share: the CONSENT bit -- a 'local' comment crosses back on the return ONLY when a
    #          persona explicitly ticked it. Default OFF (a vendor's internal notes must not
    #          ship). db._ensure_columns is fully generic, so no shim edit is needed (MED-6).
    direction: str = "local"
    external_ref: str | None = None
    share: bool = False


class VQAIgnore(SQLModel, table=True):
    """A persisted per-finding dismissal (ignore-with-reason). Findings themselves are
    computed live; this table records which ones a human waved through, keyed by
    segment + check + a stable detail signature so re-running QA keeps the dismissal."""

    __tablename__ = "vqa_ignore"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="vproject.id")
    segment_id: int = Field(index=True)
    check_key: str = ""
    detail_sig: str = ""
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VCheckConfig(SQLModel, table=True):
    """Editable severity per QA check (warning|error). Seeded with defaults; the
    settings page mutates it."""

    __tablename__ = "vcheck_config"

    id: int | None = Field(default=None, primary_key=True)
    check_key: str = Field(default="", unique=True, index=True)
    severity: str = "warning"


class VAdvisorEvent(SQLModel, table=True):
    """Audit of Tier-2 advisor accept/ignore (never gating; logged for the record)."""

    __tablename__ = "vadvisor_event"

    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(index=True)
    action: str = ""  # advisor_accepted | advisor_ignored
    suggestion: str = ""
    rationale: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class WireLog(SQLModel, table=True):
    """Every inbound and outbound wire event, with a SANITIZED reason (never a secret
    or claim token). Doubles as the operator's debugging view."""

    __tablename__ = "wire_log"

    id: int | None = Field(default=None, primary_key=True)
    direction: str = ""  # in | out
    kind: str = ""  # dispatch | return | poll | cancel | quote | quote_decision
    project_id: int | None = None
    verdict: str = ""  # accepted | rejected | ok | error | ...
    status_code: int | None = None
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
