"""store_common: shared leaf helpers (layered pure move)

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import threading

from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    Connection,
    Project,
    ProjectAutomation,
    ProjectBindingClaim,
    SyncLog,
    WebhookEvent,
)
from app.services import ids

log = logging.getLogger("loc_tms.connectors")


# --- secret encryption ---
def _fernet() -> Fernet:
    return Fernet(config.ENCRYPTION_KEY.encode())


CONFIG_LOCK = threading.Lock()


SECRET_LOCK = threading.Lock()


_REFRESH_LOCKS: dict[int, threading.Lock] = {}


_REFRESH_LOCKS_GUARD = threading.Lock()


# Every write to Connection.config serializes on this lock (store.update,
# patch_config, and the scheduler's state writes). Config is a JSON blob that
# several cards + the scheduler tick all touch; a read-modify-write from two
# threads would otherwise lose an update — including resurrecting a schedule a
# concurrent request just disabled. Holding it only for the DB read-write of one
# row keeps contention negligible.
# Distinct from CONFIG_LOCK on purpose: this guards Connection.secret_enc writes
# only (rotate_secret + OAuth refresh persistence). Config and secret_enc are
# independent JSON blobs touched by different call paths — conflating the locks
# would make a slow config save block a credential rotation (or vice versa) for
# no reason.
# Per-connection single-flight for OAuth token refresh — the THIRD distinct lock
# (see the module cross-cutting note): CONFIG_LOCK guards config, SECRET_LOCK
# guards secret_enc writes, and this guards the refresh CRITICAL SECTION so only
# one thread per cid ever refreshes at a time. Job threads + request threads race
# one connection; without this, two threads on an expired token would each POST a
# refresh, and with a ROTATING refresh token (Airtable) the loser's persisted
# token is already invalid. _REFRESH_LOCKS_GUARD only protects the tiny
# get-or-create of a cid's lock, never a refresh itself.
def _refresh_lock(cid: int) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        lk = _REFRESH_LOCKS.get(cid)
        if lk is None:
            lk = _REFRESH_LOCKS[cid] = threading.Lock()
        return lk


# --- secret_enc versioned shape (NO migration; v1-by-absence) ---
# A legacy (v1) secret is a flat dict of credentials with NO "auth_mode" key
# (e.g. {"api_key": "..."} or {"email": "...", "api_token": "..."}). An OAuth (v2)
# secret carries auth_mode="oauth" plus an "oauth" sub-dict:
#   {"auth_mode": "oauth",
#    "oauth": {"access_token", "refresh_token", "expires_at" (epoch),
#              "scope", "token_type", "obtained_at", "cloudid"?}}
# Absence of auth_mode therefore ALWAYS means "token" — every pre-OAuth secret is
# read exactly as before, untouched.
def auth_mode(secret: dict) -> str:
    """The auth mode of a decrypted secret dict: 'oauth' iff it declares so,
    else 'token' (the legacy default — absence of the key means token)."""
    return (secret or {}).get("auth_mode") or "token"


# --- Connection CRUD ---
def list_connections(kind: str | None = None, org_id=None, customer_id=None) -> list[Connection]:
    """List connections, newest first. `org_id`, when given (a scoped caller's
    org in multi-org mode — leak vector 2/10), restricts the result to that org's
    connections; None (single-tenant / the gate off) lists every connection,
    byte-identical to today.

    `customer_id` (P4, item 5), when given, restricts to that customer's OWN
    connections PLUS the global/unassigned ones (customer_id IS NULL) — the
    isolation rule: a customer's context sees its own + global, never another
    customer's. None (the default, and the global connectors index) applies no
    customer filter, byte-identical to today.

    A project automation "lane" (a private run copy of a connection that feeds several
    projects, see is_lane) is an implementation detail of the project and is never listed."""
    with Session(engine) as s:
        q = select(Connection).order_by(Connection.created_at.desc())
        if kind:
            q = q.where(Connection.kind == kind)
        if org_id is not None:
            q = q.where(Connection.org_id == org_id)
        if customer_id is not None:
            q = q.where(
                (Connection.customer_id == customer_id) | (Connection.customer_id.is_(None))
            )
        return [c for c in s.exec(q).all() if not is_lane(c)]


def is_lane(conn) -> bool:
    """True for a project automation lane: the private connection a project runs on when its
    picked connection already feeds another project (Connection.config `lane_of`)."""
    return bool((getattr(conn, "config", None) or {}).get("lane_of"))


def get(cid: int) -> Connection | None:
    with Session(engine) as s:
        return s.get(Connection, cid)


def _coerce_id(value):
    """A submitted customer_id / content_type_id -> a positive, DB-safe int, or
    None -- the single app-wide id law (app/services/ids.coerce_id). Bounds the
    int branch too (M1): a bool, 0/negative, or bigint never reaches s.get() as a
    phantom id or a raw OverflowError. Shared by create()/update()."""
    return ids.coerce_id(value)


def _resolve_content_type_id(s: Session, ct_id, org_id, customer_id):
    """Return `ct_id` iff a project of THIS connection's resolved (org_id, customer_id)
    scope may attach it — i.e. it exists, is in-org, and is GLOBAL or scoped to that
    customer; else None (SPEC-content-types phase 3, Findings 1+2).

    THE SEAM (repo law): validation lives here, not in the web route, so EVERY surface
    (web, CLI, --org operator) is confined uniformly. Keyed on the CONNECTION's target
    customer/org — never the caller's own binding — so:
      * a customer-scoped connection CAN carry that customer's own type (the functional
        case the route-only check broke), and
      * a global / other-customer connection can NOT be planted with a customer's
        private type, and an --org B operator can NOT attach an org-A type (the cross-
        org/-customer plant). An out-of-scope id is DROPPED to None (fail-closed, no
        plant) — the same silent-drop contract _valid_customer_id uses for scope."""
    if ct_id is None:
        return None
    from app.services import content_types

    ct = content_types.attachable(s, ct_id, org_id=org_id, customer_id=customer_id)
    return ct.id if ct is not None else None


def connection_project_archived(cid: int):
    """The NAME of a connection's SYNC PROJECT iff that project is ARCHIVED, else None
    (residual sweep M6). The connector translate seams consult this to SKIP a run into a
    retired project -- no paid MT / external writes. A connection with no project yet
    (never synced) returns None (nothing to skip). Best-effort; never raises."""
    try:
        with Session(engine) as s:
            conn = s.get(Connection, cid)
            if conn is None or not conn.project_id:
                return None
            proj = s.get(Project, conn.project_id)
            if proj is not None and getattr(proj, "archived_at", None) is not None:
                return proj.name
    except Exception:
        return None
    return None


# --- shared per-run write ceiling (Cluster A, adversarial audit) ------------------
# `jira_translate` and `dynamic_translate` each grew their OWN copy of the
# config.MAX_WRITES_PER_RUN ceiling (the F4 / F1 "unbounded volume" fixes) — but the
# five sibling connector translate flows (confluence/zendesk/document360/freshdesk/
# zoho) shipped WITHOUT it, so a first-ever run against a large backlog could fire
# unbounded paid MT + unbounded external writes in one shot (audit Cluster A). This
# helper is the ONE implementation of the ceiling, so a future connector #8+ that
# routes its writes through it physically cannot reintroduce the gap. jira_translate
# and dynamic_translate are retrofitted onto it too (see their call sites) so there
# is a single source of truth, not seven copies.
class WriteBudget:
    """A per-run ceiling on the number of EXTERNAL WRITES (the billable/tenant-
    touching operation — a field PUT, a comment POST, a child-page create, a
    translation-object upsert) a single connector translate run performs before it
    stops cleanly. Reads config.MAX_WRITES_PER_RUN at construction (so a test that
    patches the module constant is honored, matching the existing sites).

    Semantics mirror jira_translate exactly: the ceiling is a HARD stop, never an
    exception — whatever isn't reached this run is simply picked up on the next run
    (every flow's content-hash gate suppresses what already landed, so nothing is
    lost or re-done). `ceiling_hit` latches True the first time the budget is hit and
    is surfaced (with `write_ceiling`) in the run's structured result + a loud
    per-run log/message, so an operator sees the run stopped short rather than
    silently truncating.

    Two seams, matching the two shapes the reference sites use:
      - trim(n): BUILD-TIME cap (dynamic_translate's shape) — clamp a batch of `n`
        would-be writes to what the remaining budget allows, returning the allowed
        count and latching ceiling_hit if it clamped. Use this to trim a per-article
        `pending`-locale list BEFORE the (billable) MT + write, so MT spend is capped
        too, not just the eventual write.
      - record()/allow(): WRITE-TIME cap (jira_translate's shape) — check allow()
        before each individual write and record() after, for a flow whose writes
        don't batch cleanly.
    """

    def __init__(self, ceiling: int | None = None):
        # Same resolution the existing sites use: config.MAX_WRITES_PER_RUN. Fall back
        # to 500 ONLY when the value is genuinely absent (None) — NOT via `or 500`,
        # which would turn a deliberate 0 (an operator kill-switch / dry-run) into 500
        # paid external writes (fail-OPEN). An explicit 0 must mean zero writes; a
        # negative value also floors to 0 (fail-CLOSED) so the two agree.
        raw = ceiling if ceiling is not None else getattr(config, "MAX_WRITES_PER_RUN", None)
        self.ceiling = max(0, int(raw)) if raw is not None else 500
        self.written = 0
        self.ceiling_hit = False

    def remaining(self) -> int:
        return max(0, self.ceiling - self.written)

    def allow(self) -> bool:
        """True iff another single write fits under the ceiling. Latches ceiling_hit
        when it does NOT (so the caller can break and the run reports the stop)."""
        if self.written >= self.ceiling:
            self.ceiling_hit = True
            return False
        return True

    def record(self, n: int = 1) -> None:
        """Count `n` external writes just performed against the budget."""
        self.written += n

    def trim(self, n: int) -> int:
        """Clamp a batch of `n` would-be writes to the remaining budget. Returns the
        number allowed (0..n); records them; latches ceiling_hit iff it had to clamp
        (i.e. `n` didn't fully fit). A build-time cap — the caller drops the trimmed
        remainder so it stays pending for the next run."""
        allowed = min(n, self.remaining())
        if allowed < n:
            self.ceiling_hit = True
        self.written += allowed
        return allowed

    def summary(self) -> dict:
        """The two fields every reference site puts in its structured result."""
        return {"ceiling_hit": self.ceiling_hit, "write_ceiling": self.ceiling}

    def message(self, unit: str = "write(s)") -> str:
        """The loud per-run fragment appended to a run's human message when the
        ceiling stopped it early (mirrors jira_translate's wording)."""
        return (
            f" ⚠ Hit the per-run write ceiling ({self.ceiling} {unit}) — stopped "
            "early; remaining item(s) will be picked up on the next run."
        )


def delete(cid: int) -> str | None:
    """Delete the connection + its sync/webhook child rows. Returns the NAME of the
    connection's synced Project when one exists, else None — the Project and its pulled
    content (cached attachment Documents, translated segments, TM) are RETAINED (this
    mirrors every connector: auto-deleting the project would destroy human post-edits +
    TM). Callers surface the returned name so the operator knows a separate project
    delete is needed for full erasure."""
    retained_project = None
    lane_ids: set[int] = set()
    with Session(engine) as s:
        conn = s.get(Connection, cid)
        if conn:
            if conn.project_id:
                proj = s.get(Project, conn.project_id)
                if proj:
                    retained_project = proj.name
            # Delete the connection's child rows (SyncLog history + WebhookEvent
            # idempotency ledger, both connection_id -> connection.id) BEFORE the
            # Connection itself. Postgres has always enforced these FKs, so deleting
            # the parent first would have failed there on any connection that had
            # ever run a sync/test or accepted a webhook; the explicit child sweep
            # here (no ON DELETE CASCADE on these tables) makes the delete FK-valid.
            for child in (SyncLog, WebhookEvent):
                for row in s.exec(select(child).where(child.connection_id == cid)).all():
                    s.delete(row)
            # T3 id-map rows (ConnectorIdMap.connection_id -> connection.id). Same FK
            # reasoning as above (Postgres enforces it; no ON DELETE CASCADE). This is
            # ALSO the GDPR erase path for a connection's content: the rows reference
            # external customer content ids (source/target page ids), so deleting the
            # connection must delete them (SPEC-t3 Guardrail). idmap.delete_for_connection
            # participates in THIS transaction (session passed) so it commits atomically.
            from app.services.connectors import idmap as _idmap

            _idmap.delete_for_connection(cid, session=s)
            # Project automation: a project bound to this connection (or running on it) loses
            # its binding, the claims on the connection's items go, and every private lane
            # (a project's own copy of this connection) is deleted with it, after this commit.
            for auto in s.exec(
                select(ProjectAutomation).where(
                    (ProjectAutomation.connection_id == cid)
                    | (ProjectAutomation.run_connection_id == cid)
                )
            ).all():
                if auto.connection_id == cid and auto.run_connection_id != cid:
                    lane_ids.add(auto.run_connection_id)
                s.delete(auto)
            for claim in s.exec(
                select(ProjectBindingClaim).where(ProjectBindingClaim.connection_id == cid)
            ).all():
                s.delete(claim)
            for other in s.exec(select(Connection)).all():
                if (other.config or {}).get("lane_of") == cid:
                    lane_ids.add(other.id)
            # Flush the child deletes before deleting the parent: neither child
            # declares a SQLModel Relationship to Connection, so the unit-of-work
            # has no dependency edge to order a single commit's DELETEs by and could
            # emit DELETE connection first, tripping the FK. The flush pins order.
            s.flush()
            s.delete(conn)
            s.commit()
    for lane_id in sorted(lane_ids):
        delete(lane_id)
    return retained_project


def _log(cid: int, direction: str, ok: bool, count: int, message: str, actor: str) -> None:
    with Session(engine) as s:
        s.add(
            SyncLog(
                connection_id=cid,
                direction=direction,
                ok=ok,
                count=count,
                message=message[:500],
                actor=actor,
            )
        )
        s.commit()
