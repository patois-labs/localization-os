"""data_export: part1

Documented in: docs/architecture/data-export.md
"""

import hashlib
import hmac
import json
import logging
import os
import re
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, true
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    Customer,
    DataExportRun,
    DataExportStatus,
    Document,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemoryEvidence,
    MemoryStatus,
    MemorySuggestion,
    MemorySuggestionEvidence,
    Project,
    QueryThread,
    Segment,
    SegmentEmbedding,
    TapAction,
    VendorCustomer,
    WorkflowRun,
)
from app.services import access, reports, secrets_scrub, settings, tm_module

log = logging.getLogger("loc_tms.data_export")


MANIFEST_SCHEMA = "data-export/1"


BATCH = 1000


# keyset page size (LAW6 — no extractor materializes a whole table)
# --------------------------------------------------------------------------------------
# Errors (Train B maps these to HTTP shapes; Train A raises them from the service layer).
# --------------------------------------------------------------------------------------
class NotFound(Exception):
    """A named out-of-scope target, or a non-admin asking for workspace (LAW2). Non-
    enumerable -- the caller learns only 'not found', never whether the id exists."""


class ExportRefused(Exception):
    """The kill-switch is off (LAW9), or the plaintext-staging refuse-gate fired on an
    encrypted install (DX-208). Carries a human-readable message."""


# --------------------------------------------------------------------------------------
# Resolved scope + the extractor record.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedScope:
    scope_kind: str
    scope_ref: int | None
    org: int | None
    projects: frozenset | None  # None = unrestricted staff/operator (org-only)
    customers: frozenset  # always concrete (may be empty)
    clamp: int | None = None  # the bound customer clamp -> DataExportRun.customer_id
    include_org_shared: bool = False  # workspace only (the [G] org leg)
    include_identity: bool = False  # workspace only (§4.8 identity group + ws_only tables)
    include_audit: bool = False  # workspace + requested (drives the Train C audit slice)


@dataclass
class _Options:
    include_eval: bool = False
    include_embeddings: bool = False


@dataclass
class Extractor:
    name: str  # "<name>.jsonl"
    model: type
    strategy: str  # confinement strategy key (see _scope_clause)
    path_kind: str  # LAW13 declared confinement path (re-derived by test [P])
    cursor_col: str = "id"
    cursor_type: type = int
    history: str = "current"  # "current" | "full" (skip full-only when history="current")
    global_bearing: bool = False  # [G] tables (LAW11) -- org leg only in a caller-org workspace
    ws_only: bool = False  # workspace/admin scope only (§4.8) -- excluded from non-workspace scopes
    walkable: bool = True  # False -> emitted specially (AuditLog audit slice, Train C)
    requires_option: str = ""  # "include_eval" gate for the whole table
    tm_not_null: bool = False  # project-only TM extractor -> AND project_id IS NOT NULL (DX-309)
    current_filter: str = ""  # "lqa" | "memory" -> narrow when history="current"
    drop_cols: frozenset = frozenset()
    scrub_cols: frozenset = frozenset()
    nested_drop: dict = field(default_factory=dict)  # {col: [json_keys_to_drop]}
    # #217: URL columns (or JSON columns holding URLs) whose legacy `user:password@` userinfo
    # is stripped with the SAME helper the UI display uses (engines.display_base_url).
    url_cols: frozenset = frozenset()
    provenance_col: str = ""  # for provenance_int / eval cross-check (re-derived by [P])
    parent_col: str = ""  # for parent_fk path_kind (re-derived by [P])


# --------------------------------------------------------------------------------------
# Scope resolution (§5.3) -- the confinement core.
# --------------------------------------------------------------------------------------
def _org_val(scope: ResolvedScope):
    """The concrete org id to filter on, or None (apply no org narrowing). The poison
    BLOCKED_ORG_ID collapses to None (a broken principal narrows on nothing here -- its
    positive-membership project/customer sets are already empty, so it stays fail-closed)."""
    o = scope.org
    if isinstance(o, int) and o != access.BLOCKED_ORG_ID:
        return o
    return None


def _is_workspace_admin(user, ident) -> bool:
    """DX-209 parity: True for an admin-scoped ``lsu_`` key, a web-session admin ROLE, or the
    trusted in-process operator (``user is None``). One predicate across /api, web and CLI."""
    if user is None:
        return True  # trusted in-process operator (host CLI)
    from app.services import security

    if security.is_admin(user):
        return True  # web-session admin role
    ident = ident or {}
    scopes = ident.get("scopes") or set()
    if isinstance(scopes, str):
        scopes = set(scopes.split())
    return ident.get("kind") == "user" and "admin" in scopes


def _resolve_scope(
    session, *, user, ident, org_id, scope_kind, scope_ref, require_admin=True
) -> ResolvedScope:
    allowed = reports.allowed_project_ids(session, user, org_id)  # None = unrestricted staff
    # TA-02: for the trusted in-process operator (user=None) current_org_id is always None, so
    # the caller-supplied org_id is the ONLY org signal -- honor it (a `--org` operator run must
    # confine to that org, and `allowed_project_ids(None, org_id)` already narrows projects to it).
    org = access.current_org_id(user) if user is not None else org_id
    cust = access.current_customer_id(user)

    def org_all_customers() -> set:
        # #73 FAIL-CLOSED (TA-02): a BLOCKED principal narrows on NOTHING, so it must select the
        # EMPTY set -- never fall through to the unfiltered (maximal) customer list.
        if org == access.BLOCKED_ORG_ID:
            return set()
        q = select(Customer.id)
        if isinstance(org, int):  # a real org id (org None = single-tenant/operator see-all)
            q = q.where(Customer.org_id == org)
        return set(session.exec(q).all())

    def visible_customer_ids() -> set:
        if allowed is None:
            derived = org_all_customers()
        else:
            derived = {
                c
                for c in session.exec(
                    select(Project.customer_id).where(Project.id.in_(allowed)).distinct()
                ).all()
                if c is not None
            }
        if cust:
            derived.add(cust)  # DX-301: a bound caller ALWAYS sees its own customer
        return derived

    projects_fs = None if allowed is None else frozenset(allowed)

    if scope_kind == "self":
        return ResolvedScope(
            scope_kind="self",
            scope_ref=None,
            org=org,
            projects=projects_fs,
            customers=frozenset(visible_customer_ids()),
            clamp=cust,
        )

    if scope_kind == "project":
        proj = session.get(Project, scope_ref) if scope_ref is not None else None
        if proj is None:
            raise NotFound("project not found")
        if allowed is not None and scope_ref not in allowed:
            raise NotFound("project not found")  # LAW2 -- non-enumerable
        vis = visible_customer_ids()
        pc = proj.customer_id
        customers = frozenset({pc} & vis) if pc is not None else frozenset()
        return ResolvedScope(
            scope_kind="project",
            scope_ref=scope_ref,
            org=org,
            projects=frozenset({scope_ref}),
            customers=customers,
            clamp=cust,
        )

    if scope_kind == "customer":
        vis = visible_customer_ids()
        if scope_ref not in vis:
            raise NotFound("customer not found")  # LAW2 -- BY IDENTITY, no auto-select
        pids = tm_module.scoped_pids_for_customer(session, allowed, scope_ref)
        return ResolvedScope(
            scope_kind="customer",
            scope_ref=scope_ref,
            org=org,
            projects=frozenset(pids),
            customers=frozenset({scope_ref}),
            clamp=scope_ref,
        )

    if scope_kind == "workspace":
        # require_admin=False on the row-derived re-resolution path (TA-01): the submit-time
        # _is_workspace_admin already gated this row's CREATION, and the admin-scope signal lived
        # in `ident` (never persisted), so the handler re-derives the org-ring confinement from the
        # persisted org_id WITHOUT re-checking admin. Every SUBMIT path keeps require_admin=True.
        if require_admin and not _is_workspace_admin(user, ident):
            raise NotFound("workspace export not available")  # DX-07a: 404, not 403
        # A workspace admin reaches ANY in-org customer (the archived-customer narrative,
        # §5.3: "staff/admins for any customer in-org"), including a zero-project customer --
        # so the customer set is the caller-org's WHOLE customer list, not the project-derived
        # set. Confinement is the org ring (Customer.org_id == caller-org); never cross-org.
        return ResolvedScope(
            scope_kind="workspace",
            scope_ref=None,
            org=org,
            projects=projects_fs,
            customers=frozenset(org_all_customers() | ({cust} if cust else set())),
            clamp=cust,
            include_org_shared=True,
            include_identity=True,
        )

    raise NotFound(f"unknown scope kind: {scope_kind!r}")


# --------------------------------------------------------------------------------------
# Confinement clause helpers (all POSITIVE-membership -> NULL/global excluded by construction).
# --------------------------------------------------------------------------------------
def _proj_clause(col, scope, *, not_null=False):
    base = true() if scope.projects is None else col.in_(scope.projects)
    if not_null:
        base = and_(base, col.is_not(None))
    return base


def _proj_membership_leg(col, scope):
    """A project-membership predicate that EXCLUDES NULL/global-project rows BY CONSTRUCTION,
    even when ``scope.projects`` is None (the see-all operator) -- the DX-309 guard generalized
    (TA-03) so an OR-of-legs clause can never collapse to ``true()`` and admit a global row.
    ``col.in_(set)`` already drops NULL; ``is_not(None)`` covers the ``projects is None`` leg."""
    if scope.projects is None:
        return col.is_not(None)
    return and_(col.in_(scope.projects), col.is_not(None))


def _documents_subq(scope):
    return select(Document.id).where(_proj_clause(Document.project_id, scope))


def _query_threads_subq(scope):
    return select(QueryThread.id).where(_proj_clause(QueryThread.project_id, scope))


def _memory_cust_clause(model, scope):
    """#371: customer membership never overrides project or evidence provenance.

    Customer-wide manual memories remain shared. Derived rows require every citation to
    be visible, since their body/rationale can itself contain the cited private content.
    Parent extractors and child subqueries use the same predicates in every history mode.
    """
    base = model.customer_id.in_(scope.customers)
    if scope.projects is not None:
        base = and_(base, or_(model.project_id.is_(None), model.project_id.in_(scope.projects)))
    if model is MemoryEvent:
        for col, ids in (
            (model.document_id, _documents_subq(scope)),
            (
                model.segment_id,
                select(Segment.id).where(Segment.document_id.in_(_documents_subq(scope))),
            ),
            (
                model.workflow_run_id,
                select(WorkflowRun.id).where(WorkflowRun.document_id.in_(_documents_subq(scope))),
            ),
        ):
            base = and_(base, or_(col.is_(None), col.in_(ids)))
    elif model is MemoryEntry:
        hidden = (
            select(MemoryEvidence.id)
            .where(
                MemoryEvidence.memory_id == model.id,
                ~MemoryEvidence.event_id.in_(_memory_events_subq(scope)),
            )
            .exists()
        )
        base = and_(base, ~hidden)
    elif model is MemorySuggestion:
        from app.services.data_export_part3 import _global_workflows_subq, _workflows_subq

        hidden = (
            select(MemorySuggestionEvidence.id)
            .where(
                MemorySuggestionEvidence.suggestion_id == model.id,
                ~_suggestion_evidence_clause(scope),
            )
            .exists()
        )
        base = and_(
            base,
            or_(
                model.workflow_id.in_(_workflows_subq(scope)),
                model.workflow_id.in_(_global_workflows_subq(scope)),
            ),
            ~hidden,
        )
    return base


def _memory_events_subq(scope):
    return select(MemoryEvent.id).where(_memory_cust_clause(MemoryEvent, scope))


def _suggestion_evidence_clause(scope):
    """Validate each cited object, including malformed rows with multiple references."""
    m = MemorySuggestionEvidence
    lqa_ids = select(LQAResult.id).where(
        or_(
            LQAResult.document_id.in_(_documents_subq(scope)),
            LQAResult.segment_id.in_(
                select(Segment.id).where(Segment.document_id.in_(_documents_subq(scope)))
            ),
        )
    )
    return and_(
        or_(m.memory_id.is_not(None), m.event_id.is_not(None), m.lqa_result_id.is_not(None)),
        or_(m.memory_id.is_(None), m.memory_id.in_(_memory_subq(scope))),
        or_(m.event_id.is_(None), m.event_id.in_(_memory_events_subq(scope))),
        or_(m.lqa_result_id.is_(None), m.lqa_result_id.in_(lqa_ids)),
    )


def _memory_subq(scope):
    return select(MemoryEntry.id).where(_memory_cust_clause(MemoryEntry, scope))


def _suggestions_subq(scope):
    return select(MemorySuggestion.id).where(_memory_cust_clause(MemorySuggestion, scope))


def _tap_action_clause(scope):
    # TA-03/DX-309: the project leg excludes NULL-project rows even when scope.projects is None,
    # so a see-all operator never admits a global-project TapAction via or_(cust, true()).
    return and_(
        or_(
            and_(TapAction.project_id.is_(None), TapAction.customer_id.in_(scope.customers)),
            _proj_membership_leg(TapAction.project_id, scope),
        ),
        or_(TapAction.document_id.is_(None), TapAction.document_id.in_(_documents_subq(scope))),
    )


def _vendorcustomer_subq(scope):
    return select(VendorCustomer.vendor_id).where(VendorCustomer.customer_id.in_(scope.customers))


def _current_narrow(ex: Extractor, clause):
    """Apply the current-state narrowing for the `current` history (LAW/§4.2 notes)."""
    if ex.current_filter == "lqa":
        return and_(clause, LQAResult.withdrawn_at.is_(None))
    if ex.current_filter == "memory":
        return and_(
            clause, MemoryEntry.status == MemoryStatus.ACTIVE, MemoryEntry.superseded_at.is_(None)
        )
    return clause


# --------------------------------------------------------------------------------------
# Row projection + redaction (LAW3/LAW12).
# --------------------------------------------------------------------------------------
def _scrub_json(v):
    if isinstance(v, str):
        return secrets_scrub.scrub_secrets(v)
    if isinstance(v, dict):
        return {k: _scrub_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_json(x) for x in v]
    return v


#: `scheme://user:REST@` where REST did not parse as userinfo because the password holds an
#: unencoded "/", "?" or "#". A REST that starts with a digits-only port is a real host:port
#: whose path or query merely contains "@", and is left alone.
_URL_AMBIGUOUS_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#@:]*:([^\s@]*)@")
#: A scheme-less `user:pass@host` credential nested in config JSON (never an e-mail link).
_BARE_USERINFO = re.compile(r"^(?!mailto:)[^\s/:@]+:[^\s@]*@[^\s@]+$", re.IGNORECASE)


def _strip_url_userinfo(v, *, nested: bool = False):
    """#217: drop URL userinfo (legacy `user:password@host` credentials saved before FX-4)
    from a URL column, or from every URL-shaped string inside a JSON column. Reuses the UI's
    display helper so an export can never show more than the engines page does. A value whose
    userinfo cannot be split off cleanly (an unencoded "#", "/" or "?" in the password, or a
    scheme-less `user:pass@host` inside JSON) is masked by `settings_common.redact_url_value`,
    the config read surface's helper. Inside JSON only a string carrying "://" or that exact
    `user:pass@host` shape is touched, so prose or an e-mail address with an "@" (an
    instructions field) is left untouched."""
    if isinstance(v, str):
        if "@" not in v:
            return v
        from app.services import engines, settings_common

        if nested and "://" not in v:
            return settings_common.redact_url_value(v) if _BARE_USERINFO.match(v) else v
        shown = engines.display_base_url(v)
        m = _URL_AMBIGUOUS_USERINFO.match(shown)
        if m and not re.split(r"[/?#]", m.group(1), maxsplit=1)[0].isdigit():
            # Masked WHOLE, as redact_url_value masks any malformed value: a partial render
            # (`https://u:/p?x@h` parses as host "u", path "/p") would still show credential text.
            return settings_common.redact_url_value(m.group(0).split("://", 1)[1])
        return shown
    if isinstance(v, dict):
        return {k: _strip_url_userinfo(x, nested=True) for k, x in v.items()}
    if isinstance(v, list):
        return [_strip_url_userinfo(x, nested=True) for x in v]
    return v


def _jsonify(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return b64encode(bytes(v)).decode("ascii")
    return v


def _row_to_dict(ex: Extractor, row, options: _Options) -> dict:
    drop = set(ex.drop_cols)
    if ex.model is SegmentEmbedding and not options.include_embeddings:
        drop.add("embedding")  # §4.3 -- opaque vectors omitted unless opted in
    out = {}
    for col in ex.model.__table__.columns:
        n = col.name
        if n in drop:
            continue
        v = getattr(row, n, None)
        if n in ex.nested_drop and isinstance(v, dict):
            v = {k: x for k, x in v.items() if k not in ex.nested_drop[n]}
        if n in ex.url_cols:
            v = _strip_url_userinfo(v)  # #217: legacy URL userinfo never leaves in an export
        if n in ex.scrub_cols:
            v = _scrub_json(v)  # str -> scrub_secrets; JSON -> recurse strings
        out[n] = _jsonify(v)
    return out


# --------------------------------------------------------------------------------------
# The keyset walker (LAW6) + the single row source (LAW4).
# --------------------------------------------------------------------------------------
def _keyset_batches(session, model, base_select, cursor_col, size=BATCH):
    col = getattr(model, cursor_col)
    last = None
    while True:
        stmt = base_select
        if last is not None:
            stmt = stmt.where(col > last)
        stmt = stmt.order_by(col).limit(size)
        rows = list(session.exec(stmt).all())
        if not rows:
            return
        yield rows
        last = getattr(rows[-1], cursor_col)
        if len(rows) < size:
            return


def _applies(ex: Extractor, scope: ResolvedScope, history: str, options: _Options) -> bool:
    if not ex.walkable:
        return False
    if ex.ws_only and not scope.include_identity:
        return False
    if ex.history == "full" and history == "current":
        return False
    return not (ex.requires_option and not getattr(options, ex.requires_option, False))


# --------------------------------------------------------------------------------------
# Emitters (Train A: the JSONL backbone; CSV/XLSX are Train C).
# --------------------------------------------------------------------------------------
class JsonlEmitter:
    """The machine-first backbone emitter: one JSON object per line. Deterministic
    (``sort_keys``) so a fixed (scope, history) produces a byte-stable member (§10)."""

    kind = "jsonl"
    ext = "jsonl"

    def member_name(self, entity: str) -> str:
        return f"{entity}.{self.ext}"

    def serialize_row(self, d: dict) -> bytes:
        return (json.dumps(d, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode(
            "utf-8"
        )


_JSONL = JsonlEmitter()


# --------------------------------------------------------------------------------------
# Manifest (§6.3 -- mirrors backup_archive's manifest+sha256 shape).
# --------------------------------------------------------------------------------------
def _app_version() -> str:
    try:
        import tomllib

        with open(config.BASE_DIR / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh).get("project", {}).get("version", "unknown"))
    except Exception:
        return "unknown"


def _members_digest(members: list) -> str:
    """A container-independent artifact digest: sha256 over the sorted per-member
    ``path:sha256`` lines. Verifiable offline without re-reading the ZIP bytes (which would be
    circular, since the manifest lives inside the ZIP)."""
    h = hashlib.sha256()
    for m in sorted(members, key=lambda x: x["path"]):
        h.update(f"{m['path']}:{m['sha256']}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# Refuse-gates (LAW9 kill switch + DX-208 plaintext-staging on an encrypted install).
# --------------------------------------------------------------------------------------
def _enabled() -> bool:
    return str(settings.get("data_export_enabled", "1")) == "1"


def _s3_export_configured() -> bool:
    # S3 staging (Train C) is exempt from the plaintext refuse-gate (encrypted in transit +
    # bucket SSE). The staging class is chosen at boot by blobstore_s3_enabled (SPEC §8.3).
    return str(settings.get("blobstore_s3_enabled", "0")) == "1"


def _check_refuse_gates() -> None:
    if not _enabled():
        raise ExportRefused("data export is disabled (setting data_export_enabled=0).")
    encrypted = (
        str(settings.get("encrypt_uploads", "0")) == "1"
        or str(settings.get("encrypt_db", "0")) == "1"
    )
    allow = str(settings.get("data_export_allow_plaintext_staging", "0")) == "1"
    if encrypted and not allow and not _s3_export_configured():
        raise ExportRefused(
            "data export stages artifacts UNENCRYPTED on disk; this install encrypts data at "
            "rest. Set data_export_allow_plaintext_staging=1 to acknowledge, or configure S3 "
            "staging."
        )


# --------------------------------------------------------------------------------------
# Generation: stream one JSONL member per applicable entity into a spooled ZIP, then stage
# it through the ExportStore port (LAW6 artifact-wise, LAW7 atomic).
# --------------------------------------------------------------------------------------
def _ensure_exports_dir() -> None:
    """Create EXPORTS_DIR 0o700 (mirrors the ExportStore adapter's posture, DX-208) so the
    spooled ZIP is staged inside the tightened dir, not the DATA_DIR root (TA-08)."""
    config.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:  # noqa: SIM105
        os.chmod(config.EXPORTS_DIR, 0o700)
    except OSError:
        pass


def _file_chunks(path, size=1024 * 1024):
    with open(path, "rb") as fh:
        while True:
            b = fh.read(size)
            if not b:
                return
            yield b


def _artifact_key(run_uuid: str) -> str:
    return f"dataexport_{run_uuid}.zip"


# --------------------------------------------------------------------------------------
# The job handler (jobs.HANDLERS["data_export"]) + the synchronous host-CLI entry.
# --------------------------------------------------------------------------------------
def _audit(action: str, scope: ResolvedScope, actor, detail: str = "") -> None:
    try:
        from app.services import audit

        audit.log(
            actor=actor or "cli",
            action=action,
            target=f"scope={scope.scope_kind}:{scope.scope_ref}",
            detail=secrets_scrub.scrub_secrets(detail),
            customer_id=scope.clamp,
        )
    except Exception:  # audit must never crash an export (mirrors scheduler's guarded audit)
        log.exception("data_export audit log failed for action %s", action)


def _assert_payload_matches_row(payload: dict, row: DataExportRun) -> None:
    """Belt-and-suspenders (TA-01): the caller-supplied payload's scope TUPLE must equal the
    row's stored tuple. The row-derived resolution is the real ACL, but this fails FAST on a
    payload that tries to change scope_kind / org / customer under a reused run_uuid."""
    want = (row.scope_kind, row.scope_ref, row.org_id, row.customer_id)
    got = (
        payload.get("scope_kind"),
        payload.get("scope_ref"),
        payload.get("org"),
        payload.get("customer_id"),
    )
    if got != want:
        raise ExportRefused("data export payload scope does not match the recorded run (refused).")


def _opts_hash(history, emitters, include_audit, include_eval, include_embeddings) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "history": history,
                "emitters": sorted(emitters),
                "include_audit": bool(include_audit),
                "include_eval": bool(include_eval),
                "include_embeddings": bool(include_embeddings),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _flat_cell(v):
    """Flatten a projected value for a COLUMNAR view: a JSON column (dict/list) becomes a compact
    JSON string; scalars pass through. The value is already `_jsonify`'d (datetimes -> iso str,
    bytes -> b64 str), so only dict/list need flattening."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    return v


def _sha256_and_size(path) -> tuple:
    """Chunked sha256 + byte size of a staged member file (the backup_archive.sha256_file
    idiom) -- so a large CSV/XLSX member is hashed without being read whole into memory."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


_REAP_STATUS_KEY = "data_export_reap_status"


# --------------------------------------------------------------------------------------
# §8.4 The staged-artifact retention reaper (self-throttled by data_export_retention_hours via
# a MAC'd status row -- the audit.verify_chain_scheduled_sweep pattern, §2.3).
# --------------------------------------------------------------------------------------
def _reap_status_mac(body: str) -> str:
    """MAC over the EXACT stored status body (keyed off the session-secret keyfile, like the
    audit anchor) so a DB-only attacker cannot forge/freeze the reaper's cadence gate."""
    key = hashlib.sha256(("loc-dataexport-reap:" + config.SESSION_SECRET).encode("utf-8")).digest()
    payload = ("data-export-reap-status:" + body).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _parse_iso_naive(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _sweep_due(last, now, hours: int) -> bool:
    """Due unless there is a valid, non-future last-run whose cadence has not elapsed (a forged
    future stamp / backward clock jump therefore runs, never defers -- the llmemory/audit clamp)."""
    if last is None:
        return True
    return not (last <= now and (now - last) < timedelta(hours=hours))


def _retention_hours() -> int:
    try:
        return int(settings.get("data_export_retention_hours", "72") or 72)
    except (TypeError, ValueError):
        return 72


def _grace_minutes() -> int:
    try:
        return int(settings.get("data_export_download_grace_minutes", "30") or 30)
    except (TypeError, ValueError):
        return 30


def _fail_dead_running(s, now: datetime) -> None:
    """Fail-out a `running` DataExportRun whose dispatching Job is dead -- a crashed worker that
    left the row RUNNING with no live job (boot recovery fails the Job; this fails the row)."""
    from app.models import Job, JobStatus

    running = s.exec(
        select(DataExportRun).where(DataExportRun.status == DataExportStatus.RUNNING)
    ).all()
    for row in running:
        job = s.get(Job, row.job_id) if row.job_id else None
        # Dead = the dispatching Job FAILED (a crashed worker), or the row has no job at all.
        # A missing job_id alone is NOT dead (the row is mid-submit before job_id is stamped);
        # only a stamped-but-vanished/failed job counts.
        dead = (row.job_id is not None) and (job is None or job.status == JobStatus.FAILED)
        if dead:
            row.status = DataExportStatus.FAILED
            row.error = (row.error or "dispatching job is no longer running (reaper swept)")[:500]
            row.finished_at = row.finished_at or now
            s.add(row)


def _run_uuid_from_tmp(name: str):
    m = re.search(r"dataexport_([0-9a-f]{32})\.zip", name)
    return m.group(1) if m else None


def _has_live_run(run_uuid: str) -> bool:
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
    return row is not None and row.status in (
        DataExportStatus.QUEUED,
        DataExportStatus.RUNNING,
    )


def _unlink_run_tmp(run_uuid: str) -> None:
    """Best-effort: unlink any stranded `.tmp` sibling belonging to one run (used by the GDPR
    purge so a cancelled in-flight run leaves no plaintext temp behind)."""
    d = config.EXPORTS_DIR
    if not d.exists():
        return
    for entry in list(d.iterdir()):
        if entry.name.endswith(".tmp") and run_uuid in entry.name:
            try:  # noqa: SIM105
                entry.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------------------
# §8.4 GDPR-erase hook (DX-207/DX-311) -- delete/invalidate live staged artifacts whose scope
# COVERS an erased subject. Fail-safe OVER-deletion; ORG-CONFINED (never another org's dumps).
# --------------------------------------------------------------------------------------
def _row_covers(row: DataExportRun, customer_ids: set, project_ids: set) -> bool:
    """Does one live DataExportRun's scope contain an erased subject (already org-matched)? A
    WORKSPACE dump covers its whole org (any subject in it); a customer/project dump covers the
    subject only when its clamp/ref matches the subject's customer or project. Fail-safe.

    A SELF dump is treated like a workspace dump (fail-safe over-deletion, files-03): its scope
    is "every project the caller could see at staging time", which `_resolve_scope` derives
    from membership/role and does NOT persist, so the row alone cannot prove the erased
    project was outside it. Matching on the `customer_id` clamp missed every unbound caller's
    self dump (clamp NULL), the default scope of the web form, CLI and /api."""
    if row.scope_kind in ("workspace", "self"):
        return True
    if customer_ids:
        if row.customer_id in customer_ids:
            return True
        if row.scope_kind == "customer" and row.scope_ref in customer_ids:
            return True
    return bool(project_ids and row.scope_kind == "project" and row.scope_ref in project_ids)
