"""Project automation (MVP part A): a project's content source and its run schedule.

A project binds to a connection (the place its content is pulled from and pushed back to),
and may run on a schedule: UTC times of day and days of week, or an interval, in delta or full
mode. Runs ride the existing `connector_translate` job, so progress, history, failure capture,
restart recovery, the archive / AI opt-out / inactive-org skips and the review gate stay the job
engine's and the connector flows' business.

ONE CONNECTION, MANY PROJECTS. Per-connection state (content hashes,
watermarks, the id map, pending sets) cannot be shared by two projects, so:
  * the first project to bind a connection that feeds no project yet adopts it directly;
  * any further project gets a private "lane": a clone of the picked connection (same
    credential, kept in step with it, own state) that only that project runs on. A lane is a
    Connection row with config `lane_of`; it is never listed on the connection screens.
  * the two projects' write-backs cannot collide: the first project to reach an external item
    owns it (ProjectBindingClaim), a later project skips it and the conflict is logged on the
    run and in the audit trail. Kinds outside the shared flow engine (airtable, jira and the
    git trio) take no claims; for them two bindings with an identical scope are refused.

Safety model (a run spends money and writes to live systems, so losing a run is always better
than duplicating one):
  * nothing is enabled by default and enabling a schedule never fires immediately;
  * consume-slot-first: next_at moves forward BEFORE the job is submitted;
  * `jobs.submit_unique` is the overlap guard (one in-flight run per connection);
  * the global kill switch `sync_scheduler_enabled`, the per-kind switch
    `connector_disabled_kinds`, archived / AI-off projects and suspended orgs are all honoured;
  * the project's monthly budget is checked before each run (and again when it starts); an
    optional per-run USD cap stops paid calls once the run has spent it;
  * after N consecutive failed runs (default 3) or when the connection needs a reconnect the
    automation pauses itself until a person resumes it.

Documented in: docs/architecture/project-automation.md
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine
from app.models import (
    Connection,
    ConnStatus,
    Job,
    JobStatus,
    Project,
    ProjectAutomation,
    ProjectBindingClaim,
    TargetLocale,
)
from app.services import access, audit, automation_schedule, ids, jobs, settings, spend
from app.services import connectors as registry
from app.services.connectors import run_ctx
from app.services.connectors import store as conn_store
from app.services.connectors.store_common import (
    config_locales,
    dedupe_locales,
    in_connection_spelling,
)
from app.services.connectors.store_common_leaf import is_lane

log = logging.getLogger("loc_tms.project_automation")

# Kinds that run through the shared flow engine (flow.run_translate_flow): they honour a FULL
# pull (hash suppression off) and write-back claims. Airtable, Jira and the git trio keep their
# own loops: delta only, no claims.
NON_FLOW_KINDS = frozenset({"airtable", "jira", "github", "gitlab", "bitbucket"})

MODES = ("delta", "full")
DEFAULT_AUTO_PAUSE_AFTER = 3
MAX_AUTO_PAUSE_AFTER = 50
MAX_RUN_CAP_USD = 100000.0
#: A scheduled slot that fires later than this after its time was MISSED (the install was down or
#: the scheduler off): it runs once as a catch-up run. A slot a tick or two late is just a slot.
CATCH_UP_GRACE = timedelta(minutes=10)
_MAX_MISSED_COUNT = 5000

# Config keys a run writes (hashes, watermarks, schedule and webhook state, branch records,
# pending sets, read-back fingerprints). A lane starts with none of them.
_STATE_SUFFIXES = (
    "_hashes",
    "_watermark",
    "_pending",
    "_branch_state",
    "_target_prints",
    "_edited_src",
    "_cursor",
)
_STATE_PREFIXES = ("sync_", "webhook", "airtable_webhook", "lane_")
_STATE_KEYS = frozenset({"jira_pending", "last_sync", "lane_of"})

# Scope keys a MANAGER may set on a lane or an adopted connection: they narrow what is read
# inside the same credential and destination. Everything else a connector declares (hosts,
# roots, projects, repositories, JQL, live-edit switches) is admin only, like the connection
# screens (store_common.SCOPE_ADMIN_FIELDS / LIVE_EDIT_ADMIN_FIELDS).
NARROW_KEYS: dict[str, tuple[str, ...]] = {
    "airtable": ("view", "max_records"),
    "jira": ("max_results",),
    "confluence": ("max_pages",),
    "zendesk": ("label_names", "section_id", "category_id", "max_articles"),
    "document360": ("category_id", "max_articles"),
    "freshdesk": ("category_id", "folder_id", "max_articles"),
    "zoho": ("department_id", "category_id", "max_articles"),
    "intercom": ("help_center_id", "max_articles"),
    "salesforce": ("record_type_id", "max_articles"),
    "notion": ("source_depth", "max_pages"),
    "helpscout": ("max_articles",),
    "strapi": ("content_types", "fields", "exclude_fields", "max_entries"),
    "contentful": ("content_types", "fields", "exclude_fields", "max_entries"),
    "shopify": ("resource_types", "fields", "exclude_fields", "max_entries"),
    "hubspot": ("content_types", "fields", "exclude_fields", "max_entries"),
    "gdocs": ("include_subfolders", "max_files"),
    "akeneo": ("content_types", "max_entries"),
    "mcp_cms": ("max_entries",),
    "servicenow": ("max_articles",),
    "msgraph": ("include_subfolders", "extensions", "max_files"),
    "plytix": ("skus", "max_entries"),
    "drupal": ("resource_types", "fields", "exclude_fields", "max_entries"),
    "github": ("source_paths",),
    "gitlab": ("source_paths",),
    "bitbucket": ("source_paths",),
    "sanity": ("document_types", "fields", "exclude_fields", "max_entries"),
    "sitecore": ("fields", "exclude_fields", "max_items"),
    "wordpress": ("post_types", "source_category", "source_tag", "source_status", "max_articles"),
}
_ALWAYS_NARROW = ("target_locales",)
# Config keys that name a connection's target languages. A lane keeps its own (the project's).
_LANGUAGE_KEYS = ("target_locales", "target_locale", "language_set_id")

# Fields that do not decide WHICH items a connection reads (locales, flags, caps): left out of
# the scope fingerprint that refuses two identical bindings on one connection.
_FINGERPRINT_SKIP = frozenset(
    {
        "target_locales",
        "target_locale",
        "source_locale",
        "source_lang",
        "source_language",
        "harvest_context",
        "auto_translate_on_change",
        "publish",
        "draft",
        "translate_comments",
        "context_brief",
    }
)


class AutomationError(Exception):
    """A request the person can fix. `status` hints the HTTP code a shell should use
    (404 missing or not visible, 409 state conflict, 400 bad input, 403 not allowed)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.utcnow()


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _is_state_key(key: str) -> bool:
    k = str(key)
    return k in _STATE_KEYS or k.endswith(_STATE_SUFFIXES) or k.startswith(_STATE_PREFIXES)


def clean_config(cfg: dict | None) -> dict:
    """A connection's config without the state keys a run owns (a lane starts fresh)."""
    return {k: v for k, v in (cfg or {}).items() if not _is_state_key(k)}


def scope_fingerprint(kind: str, cfg: dict | None) -> str:
    """A stable digest of what a connection's config says it reads, for the identical-scope
    refusal on kinds that take no claims."""
    sp = registry.spec(kind)
    names = [f.name for f in (sp.config_fields if sp else []) if not f.secret]
    parts = []
    for n in sorted(names):
        if n in _FINGERPRINT_SKIP or n.startswith("max_"):
            continue
        parts.append(f"{n}={str((cfg or {}).get(n) or '').strip().lower()}")
    return _sha("|".join(parts))


# ---------------------------------------------------------------------------------------
# lookups and visibility
# ---------------------------------------------------------------------------------------
def get(session: Session, project_id: int) -> ProjectAutomation | None:
    pid = ids.coerce_id(project_id)
    if pid is None:
        return None
    return session.exec(
        select(ProjectAutomation).where(ProjectAutomation.project_id == pid)
    ).first()


def _project(session: Session, project_id) -> Project:
    pid = ids.coerce_id(project_id)
    proj = session.get(Project, pid) if pid is not None else None
    if proj is None:
        raise AutomationError("No such project.", 404)
    return proj


def connection_visible_to_project(conn: Connection | None, project: Project) -> bool:
    """May `project` use `conn`? Same org (when multi-org is on), and the connection is global or
    belongs to the project's customer. A lane is never a valid pick."""
    if conn is None or is_lane(conn):
        return False
    if not registry.kind_enabled(conn.kind):
        return False
    if access.multi_org_active() and getattr(conn, "org_id", None) != getattr(
        project, "org_id", None
    ):
        return False
    c_cust = getattr(conn, "customer_id", None)
    p_cust = getattr(project, "customer_id", None)
    return not (c_cust is not None and c_cust != p_cust)


def module_enabled() -> bool:
    """The project automation kill switch (setting project_automation_enabled, default on)."""
    return settings.get("project_automation_enabled", "1") == "1"


def _require_enabled() -> None:
    if not module_enabled():
        raise AutomationError(
            "Project automation is switched off on this install "
            "(setting project_automation_enabled=0).",
            403,
        )


def owns_schedule(connection_id: int) -> bool:
    """True when an ENABLED project automation uses this connection (as the picked connection or
    as its own copy), so the connection's own interval schedule (scheduler.tick) must not fire
    it as well: one schedule per connection."""
    with Session(engine) as s:
        return (
            s.exec(
                select(ProjectAutomation.id).where(
                    (ProjectAutomation.run_connection_id == connection_id)
                    | (ProjectAutomation.connection_id == connection_id),
                    ProjectAutomation.enabled == True,  # noqa: E712
                )
            ).first()
            is not None
        )


# ---------------------------------------------------------------------------------------
# scope overrides
# ---------------------------------------------------------------------------------------
_LIST_KEYS = frozenset(
    {
        "content_types",
        "fields",
        "resource_types",
        "document_types",
        "post_types",
        "source_paths",
        "extensions",
        "skus",
        "label_names",
        "target_locales",
        "source_category",
        "source_tag",
    }
)


def _items(value) -> set:
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = str(value or "").replace("\n", ",").replace(";", ",").split(",")
    return {p.strip().lower() for p in parts if p.strip()}


def _truthy(v) -> bool:
    return str(v if v is not None else "").strip().lower() in ("1", "true", "yes", "on")


def _widens(key: str, new, old) -> bool:
    """Would replacing the connection's current value `old` with `new` read MORE than before?
    An empty old value means "everything" (nothing to widen); an empty new one after a set old
    one means everything again (widening)."""
    new_s = str(new if new is not None else "").strip()
    old_s = str(old if old is not None else "").strip()
    if key == "exclude_fields":  # excluding MORE is narrower
        return not _items(old) <= _items(new)
    if key in _LIST_KEYS:
        if not old_s:
            return False
        return not new_s or not _items(new) <= _items(old)
    if key.startswith("max_") or key == "source_depth":
        if not old_s:
            return False
        try:
            return not new_s or float(new_s) > float(old_s)
        except ValueError:
            return True
    if key == "include_subfolders":
        return _truthy(new) and not _truthy(old)
    # ids, views, statuses: a set value may not change to another one (or be cleared)
    return bool(old_s) and new_s.lower() != old_s.lower()


def _scope_refusal(kind: str, scope: dict, is_admin: bool, current: dict | None = None) -> str:
    """Why this caller may not set `scope` on a connection whose settings are `current`. Admins
    may set any declared setting; a manager may only NARROW (a subset of the list, a lower
    number, the same id), never widen: widening is admin only (spec 6.3)."""
    sp = registry.spec(kind)
    declared = {f.name for f in (sp.config_fields if sp else []) if not f.secret}
    for key in scope:
        if str(key).endswith("_branch_state") or _is_state_key(key):
            return f"'{key}' is run state, not a scope setting."
        if key not in declared:
            return f"'{key}' is not a setting of a {kind} connection."
    if is_admin:
        return ""
    allowed = set(NARROW_KEYS.get(kind, ())) | set(_ALWAYS_NARROW)
    bad = [k for k in scope if k not in allowed]
    if bad:
        return (
            f"Only an admin can change {', '.join(sorted(map(str, bad)))} on a {kind} "
            "connection (it decides what the saved credential reads and writes). "
            f"A manager can narrow: {', '.join(sorted(allowed))}."
        )
    wide = [k for k, v in scope.items() if _widens(k, v, (current or {}).get(k))]
    if wide:
        return (
            f"Only an admin can widen {', '.join(sorted(map(str, wide)))} on a {kind} connection: "
            "a manager can only narrow what the connection already reads (a subset of the list, "
            "a lower number, the same id)."
        )
    return ""


# ---------------------------------------------------------------------------------------
# lane credential sync (feeder is the authority; an OAuth refresh made on the lane flows back)
# ---------------------------------------------------------------------------------------
def _sync_credential_to_lane(feeder_id: int, lane_id: int, scope_keys=()) -> None:
    """Make the lane follow the picked connection before a run: its credential, its posture (every
    setting that is not run state and not one of this project's own scope settings: publish,
    draft, hosts, locales, switches), its customer and org, and its health (a lane stuck in
    REAUTH recovers when the picked connection is reconnected, the only screen a person can
    reach). Per-project and kept on the lane: the scope settings and all run state."""
    with conn_store.CONFIG_LOCK, conn_store.SECRET_LOCK, Session(engine) as s:
        feeder, lane = s.get(Connection, feeder_id), s.get(Connection, lane_id)
        if feeder is None or lane is None:
            return
        changed = False
        sig = _sha(feeder.secret_enc)
        old = dict(lane.config or {})
        keep = {k: v for k, v in old.items() if _is_state_key(k) or k in set(scope_keys)}
        cfg = {**clean_config(feeder.config), **keep}
        cfg["lane_of"], cfg["lane_project_id"] = old.get("lane_of"), old.get("lane_project_id")
        cfg["lane_cred_sig"] = sig
        # the lane's languages are the PROJECT's (sync_run_locales), never the picked connection's
        for lang_key in _LANGUAGE_KEYS:
            if lang_key in old:
                cfg[lang_key] = old[lang_key]
            else:
                cfg.pop(lang_key, None)
        if cfg != old:
            lane.config = cfg
            changed = True
        if lane.secret_enc != feeder.secret_enc:
            lane.secret_enc = feeder.secret_enc
            changed = True
        for attr in ("customer_id", "org_id", "content_type_id"):
            if getattr(lane, attr) != getattr(feeder, attr):
                setattr(lane, attr, getattr(feeder, attr))
                changed = True
        if feeder.status == ConnStatus.CONNECTED and lane.status in (
            ConnStatus.REAUTH,
            ConnStatus.ERROR,
        ):
            lane.status, lane.last_message = ConnStatus.CONNECTED, ""
            changed = True
        if changed:
            s.add(lane)
            s.commit()


def _sync_credential_from_lane(feeder_id: int, lane_id: int) -> None:
    """After a run: when the lane's token was refreshed (the provider rotated it), hand the new
    credential back to the picked connection, but only if that connection still holds the
    credential the lane started from (never overwrite a rotation a person just made)."""
    with conn_store.CONFIG_LOCK, conn_store.SECRET_LOCK, Session(engine) as s:
        feeder, lane = s.get(Connection, feeder_id), s.get(Connection, lane_id)
        if feeder is None or lane is None:
            return
        cfg = dict(lane.config or {})
        if lane.secret_enc == feeder.secret_enc:
            return
        if _sha(feeder.secret_enc) != cfg.get("lane_cred_sig"):
            return
        feeder.secret_enc = lane.secret_enc
        cfg["lane_cred_sig"] = _sha(lane.secret_enc)
        lane.config = cfg
        s.add(feeder)
        s.add(lane)
        s.commit()


# ---------------------------------------------------------------------------------------
# bind / unbind
# ---------------------------------------------------------------------------------------
def _lane_for(session: Session, feeder_id: int, project_id: int) -> Connection | None:
    for c in session.exec(select(Connection)).all():
        cfg = c.config or {}
        if cfg.get("lane_of") == feeder_id and cfg.get("lane_project_id") == project_id:
            return c
    return None


def _shape_or_refuse(project_id, kind: str, *, session: Session | None = None) -> str | None:
    """Connected-project shape rule for a bind of a `kind` source (see
    connected_projects.shape_for_bind): the project_type to apply, None for no change; a refusal
    becomes a 400 AutomationError."""
    from app.services import connected_projects

    def _check(s: Session):
        project = s.get(Project, ids.coerce_id(project_id))
        if project is None:
            return None
        try:
            return connected_projects.shape_for_bind(s, project, kind)
        except connected_projects.IntakeError as exc:
            raise AutomationError(str(exc), 400) from exc

    if session is not None:
        return _check(session)
    with Session(engine) as s:
        return _check(s)


def bind(
    project_id: int,
    connection_id: int | None = None,
    *,
    actor: str = "",
    is_admin: bool = False,
    clone: bool = False,
    scope: dict | None = None,
    new_connection: dict | None = None,
    org_id=None,
    bound_customer_id=None,
) -> dict:
    """Bind a project to a connection as its content source and push target.

    `connection_id` picks an existing connection; `new_connection` ({kind, name, config,
    secret}) creates one first and binds it; `clone=True` gives the project its own copy of the
    picked connection (a lane) even when the connection feeds no project yet. `scope` overrides
    scope settings for this project (admins any declared setting, managers the narrowing ones).
    Raises AutomationError."""
    _require_enabled()
    with Session(engine) as s:
        project = _project(s, project_id)
        if get(s, project.id) is not None:
            raise AutomationError(
                "This project already has a content source. Unbind it first.", 409
            )
        if getattr(project, "inbox_key", None):
            raise AutomationError("An inbox project cannot take a content source.", 400)
        p_org, p_cust, pid, pname = project.org_id, project.customer_id, project.id, project.name

    created_here = False
    if new_connection:
        if connection_id is not None:
            raise AutomationError("Pick an existing connection or create a new one, not both.")
        kind = str(new_connection.get("kind") or "").strip().lower()
        if kind not in registry.REGISTRY or not registry.kind_enabled(kind):
            raise AutomationError("Unknown or disabled connector kind.", 400)
        if registry.is_archived(kind):
            raise AutomationError("That connector kind cannot take new connections.", 400)
        _shape_or_refuse(project_id, kind)  # refuse a shape mismatch before creating anything
        cfg = new_connection.get("config") or {}
        if "secret" in new_connection or not isinstance(cfg, dict):
            raise AutomationError(
                "A connection created here carries no credential (config must be an object); "
                "set the credential on the connection afterwards.",
                400,
            )
        try:
            conn = conn_store.create(
                kind,
                str(new_connection.get("name") or "").strip() or f"{pname} ({kind})",
                cfg,
                {},
                actor=actor,
                org_id=org_id if org_id is not None else p_org,
                customer_id=p_cust,
                bound_customer_id=bound_customer_id,
            )
        except registry.ConnectorError as exc:
            raise AutomationError(str(exc), 400) from exc
        connection_id = conn.id
        created_here = True
        audit.log(
            actor=actor or "system",
            action="connector.create",
            target=f"connection:{conn.id}",
            detail=f"kind:{kind} via project automation (project:{pid})",
            customer_id=p_cust,
        )

    cid = ids.coerce_id(connection_id)
    with Session(engine) as s:
        project = _project(s, pid)
        feeder = s.get(Connection, cid) if cid is not None else None
        if not connection_visible_to_project(feeder, project):
            raise AutomationError("No such connection.", 404)
        kind = feeder.kind
        shape_type = _shape_or_refuse(project_id, kind, session=s)
        scope = dict(scope or {})
        if scope:
            msg = _scope_refusal(kind, scope, is_admin, current=feeder.config)
            if msg:
                raise AutomationError(msg, 403)

        existing_direct = s.exec(
            select(ProjectAutomation).where(ProjectAutomation.run_connection_id == feeder.id)
        ).first()
        same_project = feeder.project_id == project.id
        free = feeder.project_id is None and existing_direct is None
        # An ORGANIZATION-WIDE connection (no customer) may be bound by a manager (round 2,
        # decision 10), but a manager never changes the shared connection itself: the project
        # always gets its own lane (same credential, own scope / hashes / id map), so one
        # project's scope or ownership can never leak into the shared connection or into the
        # next project that binds it. An admin keeps the direct adoption.
        org_wide = feeder.customer_id is None
        use_lane = bool(clone) or not (same_project or free)
        if org_wide and not is_admin and not same_project:
            use_lane = True
        run_conn_id = feeder.id
        lane_reused = False
        if use_lane:
            lane = _lane_for(s, feeder.id, project.id)
            if lane is not None:
                run_conn_id, lane_reused = lane.id, True
        feeder_cfg = dict(feeder.config or {})
        feeder_name, f_org, f_cust = feeder.name, feeder.org_id, feeder.customer_id

    # A kind outside the shared flow engine takes no per-item claims, so two projects on one
    # connection could write the same item: only one project may use such a connection.
    if kind in NON_FLOW_KINDS:
        with Session(engine) as s:
            if s.exec(
                select(ProjectAutomation.id).where(ProjectAutomation.connection_id == cid)
            ).first():
                raise AutomationError(
                    f"Another project already uses this {kind} connection, and {kind} writes "
                    "are not claimed per item, so one connection can serve only one project. "
                    "Use another connection.",
                    409,
                )

    if use_lane and not lane_reused:
        lane_cfg = {
            **clean_config(feeder_cfg),
            **scope,
            "lane_of": cid,
            "lane_project_id": pid,
        }
        secret = conn_store.dec_secret(_secret_of(cid))
        try:
            lane = conn_store.create(
                kind,
                f"{feeder_name} - project {pid}",
                {k: v for k, v in lane_cfg.items() if not str(k).startswith("lane_cred")},
                secret,
                actor=actor,
                org_id=f_org,
                customer_id=f_cust,
            )
        except registry.ConnectorError as exc:
            raise AutomationError(str(exc), 400) from exc
        run_conn_id = lane.id
        _sync_credential_to_lane(cid, run_conn_id, list(scope))
    apply_scope_after = bool(scope) and not (use_lane and not lane_reused)

    with Session(engine) as s:
        run_conn = s.get(Connection, run_conn_id)
        if run_conn is not None and run_conn.project_id is None:
            run_conn.project_id = pid  # the connection (or lane) now imports into this project
            s.add(run_conn)
        row = ProjectAutomation(
            project_id=pid,
            org_id=p_org,
            connection_id=cid,
            run_connection_id=run_conn_id,
            scope=scope or None,
            created_by=actor,
        )
        s.add(row)
        # Connected projects: a keys-shaped source on a still-EMPTY project makes it key_based
        # (the one allowed change of project_type), in the same commit as the binding.
        old_shape = None
        if shape_type:
            from app.services import connected_projects

            old_shape = connected_projects.apply_shape(s, _project(s, pid), shape_type)
        try:
            s.commit()
        except IntegrityError as exc:
            s.rollback()
            raise AutomationError("This project already has a content source.", 409) from exc
        s.refresh(row)
        out_id = row.id
    if old_shape:
        connected_projects.audit_shape(pid, p_cust, old_shape, shape_type, kind=kind, actor=actor)

    if apply_scope_after:
        # Adopted (or reused lane) connection: apply the scope through the normal update path,
        # AFTER the binding exists; a failure removes the binding again so nothing half-applied
        # stays behind on a connection other projects use.
        try:
            with connection_follows_project():  # a bind's scope, not a language edit
                conn_store.update(run_conn_id, "", scope, None, actor=actor)
        except Exception as exc:
            with Session(engine) as s:
                gone = s.get(ProjectAutomation, out_id)
                if gone is not None:
                    s.delete(gone)
                    s.commit()
            if isinstance(exc, registry.ConnectorError):
                raise AutomationError(str(exc), 400) from exc
            raise

    # Connected projects, setup step 3: a connected project bound while it has no target
    # language at all takes the connection's own target locales (through the normal add path).
    _fill_empty_connected_languages(pid, feeder_cfg, org_id=f_org, actor=actor)
    sync_run_locales(pid, actor=actor)  # the run connection's languages follow the project
    audit.log(
        actor=actor or "system",
        action="automation.clone" if use_lane else "automation.bind",
        target=f"project:{pid}",
        detail=(
            f"connection:{cid} run_connection:{run_conn_id}"
            f"{' new' if created_here else ''}"
            f"{' org_wide' if org_wide else ''}"
            f"{' scope:' + ','.join(sorted(scope)) if scope else ''}"
        ),
        customer_id=p_cust,
    )
    with Session(engine) as s:
        return view(s, s.get(ProjectAutomation, out_id))


def _fill_empty_connected_languages(
    project_id: int, feeder_cfg: dict, *, org_id=None, actor: str = ""
) -> list[str]:
    """SPEC-connected-projects section 6 step 3: languages default from the connection's target
    locales when the project has none. Only for a CONNECTED project (Project.intake) with no
    TargetLocale row at all (a stopped one counts as a language); every other bind is untouched.
    Uses the connection's own Language Set or its explicit target_locales / target_locale, never
    the run-time "fr-FR" fallback. Each locale goes through content.add_target_locale (catalog
    validation, the bound-project hook); a locale the catalog refuses is skipped. After this the
    project's list is the source of truth (decision 4). Never raises into bind."""
    try:
        from app.services import content, languages

        with Session(engine) as s:
            project = s.get(Project, project_id)
            if project is None or getattr(project, "intake", "upload") != "connected":
                return []
            if s.exec(select(TargetLocale.id).where(TargetLocale.project_id == project_id)).first():
                return []
            cfg = feeder_cfg or {}
            ls = None
            if cfg.get("language_set_id"):
                ls = languages.get(cfg.get("language_set_id"), org_id)
            if ls and ls.target_locales:
                wanted = list(ls.target_locales)
            else:
                wanted = languages.parse_locales(
                    cfg.get("target_locales") or cfg.get("target_locale") or ""
                )
            source = str(project.source_locale or "").strip().lower()
            added = []
            for loc in wanted:
                if str(loc).strip().lower() == source:
                    continue
                try:
                    if content.add_target_locale(s, project_id, loc, actor=actor):
                        added.append(loc)
                except Exception:  # an unknown code is skipped, never fails the bind
                    log.info("Skipped default language %r for project %s", loc, project_id)
            return added
    except Exception:
        log.exception("Could not default the languages of connected project %s", project_id)
        return []


def _secret_of(cid: int) -> str:
    with Session(engine) as s:
        c = s.get(Connection, cid)
        return c.secret_enc if c else ""


def unbind(project_id: int, *, actor: str = "") -> dict:
    """Remove the binding. Stops the automation, releases the project's item claims and keeps
    the run connection (with its state) so the same project can bind it again."""
    with Session(engine) as s:
        project = _project(s, project_id)
        row = get(s, project.id)
        if row is None:
            raise AutomationError("This project has no content source.", 404)
        pid, cid, cust = project.id, row.connection_id, project.customer_id
        if active_job(s, row) is not None:
            raise AutomationError(
                "A run is in progress for this project. Unbind when it has finished.", 409
            )
        s.exec(delete(ProjectBindingClaim).where(ProjectBindingClaim.project_id == pid))
        s.delete(row)
        s.commit()
    audit.log(
        actor=actor or "system",
        action="automation.unbind",
        target=f"project:{pid}",
        detail=f"connection:{cid}",
        customer_id=cust,
    )
    return {"project_id": pid, "connection_id": cid, "bound": False}


# ---------------------------------------------------------------------------------------
# configure / pause / resume
# ---------------------------------------------------------------------------------------
def configure(
    project_id: int,
    *,
    actor: str = "",
    is_admin: bool = False,
    enabled=None,
    times=None,
    days=None,
    interval_minutes=None,
    mode: str | None = None,
    auto_pause_after=None,
    run_cap_usd="__unset__",
    move_schedule: bool = False,
    now: datetime | None = None,
) -> dict:
    """Change the schedule and run settings. Only the fields passed change. Turning the schedule
    on never fires immediately: next_at is the next slot after `now`."""
    now = now or _now()
    if enabled:
        _require_enabled()
    with Session(engine) as s:
        project = _project(s, project_id)
        row = get(s, project.id)
        if row is None:
            raise AutomationError(
                "This project has no content source. Bind a connection first.", 404
            )
        conn = s.get(Connection, row.run_connection_id)
        kind = conn.kind if conn else ""
        before = (
            list(row.times or []),
            list(row.days or []),
            row.interval_minutes or 0,
            bool(row.enabled),
        )

        t = list(row.times or []) if times is None else times
        d = list(row.days or []) if days is None else days
        iv = row.interval_minutes if interval_minutes is None else interval_minutes
        if times is not None and times != [] and times != "" and interval_minutes is None:
            iv = 0  # switching to times of day clears a stored interval
        if interval_minutes not in (None, 0, "", "0") and times is None:
            t, d = [], []  # switching to an interval clears stored times and days
        try:
            t, d, iv = automation_schedule.validate(t, d, iv)
        except automation_schedule.ScheduleError as exc:
            raise AutomationError(str(exc), 400) from exc

        if mode is not None:
            m = str(mode).strip().lower()
            if m not in MODES:
                raise AutomationError("mode must be 'delta' or 'full'.", 400)
            if m == "full" and kind in NON_FLOW_KINDS:
                raise AutomationError(
                    f"A full pull is not available for {kind} connections (they always re-check "
                    "only what changed).",
                    400,
                )
            row.mode = m
        if auto_pause_after is not None:
            try:
                n = int(auto_pause_after)
            except (TypeError, ValueError):
                raise AutomationError("auto_pause_after must be a whole number.", 400) from None
            if isinstance(auto_pause_after, bool) or not 1 <= n <= MAX_AUTO_PAUSE_AFTER:
                raise AutomationError(
                    f"auto_pause_after must be between 1 and {MAX_AUTO_PAUSE_AFTER}.", 400
                )
            row.auto_pause_after = n
        if run_cap_usd != "__unset__":
            if run_cap_usd in (None, "", 0, "0"):
                row.run_cap_usd = None
            else:
                try:
                    cap = float(run_cap_usd)
                except (TypeError, ValueError):
                    raise AutomationError("run_cap_usd must be a number.", 400) from None
                if isinstance(run_cap_usd, bool) or not (0 < cap <= MAX_RUN_CAP_USD) or cap != cap:
                    raise AutomationError(
                        f"run_cap_usd must be more than 0 and at most {MAX_RUN_CAP_USD:g}, "
                        "or empty for no cap.",
                        400,
                    )
                row.run_cap_usd = round(cap, 6)

        row.times, row.days, row.interval_minutes = (t or None), (d or None), iv
        scheduled = automation_schedule.has_schedule(t, d, iv)
        want_enabled = row.enabled if enabled is None else bool(enabled)
        if want_enabled and not scheduled:
            raise AutomationError(
                "Add times of day or an interval before turning the schedule on.", 400
            )
        if want_enabled and not row.enabled:
            if kind == "jira" and not is_admin and _auth_required():
                raise AutomationError("Only an admin can turn on a Jira schedule.", 403)
            if conn is not None and (conn.config or {}).get("sync_every_minutes"):
                if not move_schedule:
                    raise AutomationError(
                        "This connection has its own interval schedule. Pass move_schedule to "
                        "replace it with this project's schedule (one schedule per connection).",
                        409,
                    )
                conn_store.patch_config(
                    conn.id, remove=("sync_every_minutes", "sync_next_at"), actor=actor
                )
                audit.log(
                    actor=actor or "system",
                    action="connector.schedule",
                    target=f"connection:{conn.id}",
                    detail="moved to project automation",
                    customer_id=getattr(conn, "customer_id", None),
                )
        row.enabled = want_enabled
        after = (list(t or []), list(d or []), iv or 0, bool(want_enabled))
        if not want_enabled or not scheduled:
            row.next_at = None
        elif after != before or row.next_at is None:
            # only a changed schedule (or turning it on) moves the next slot: saving a cap or
            # the mode must not push an interval run out by a full interval
            row.next_at = automation_schedule.next_fire(t, d, iv, now)
        row.updated_at = now
        s.add(row)
        s.commit()
        s.refresh(row)
        pid, cust, detail = row.project_id, project.customer_id, _config_detail(row)
        out = view(s, row)
    audit.log(
        actor=actor or "system",
        action="automation.set",
        target=f"project:{pid}",
        detail=detail,
        customer_id=cust,
    )
    return out


def _auth_required() -> bool:
    from app.services import security

    return security.auth_required()


def _config_detail(row: ProjectAutomation) -> str:
    return (
        f"enabled:{int(bool(row.enabled))} mode:{row.mode} "
        f"{automation_schedule.describe(row.times, row.days, row.interval_minutes)} "
        f"cap:{row.run_cap_usd if row.run_cap_usd is not None else 'none'} "
        f"pause_after:{row.auto_pause_after}"
    )


def pause(project_id: int, *, reason: str = "", actor: str = "", automatic: bool = False) -> dict:
    with Session(engine) as s:
        project = _project(s, project_id)
        row = get(s, project.id)
        if row is None:
            raise AutomationError("This project has no content source.", 404)
        row.paused_at = row.paused_at or _now()
        row.paused_reason = (reason or ("Paused by a person." if not automatic else "")).strip()[
            :300
        ]
        row.updated_at = _now()
        s.add(row)
        if automatic:
            _notify(
                "automation.paused",
                project,
                {"dedup": f"paused-{row.id}-{row.paused_at.isoformat()}", "at": _now()},
                s,
            )
        s.commit()
        s.refresh(row)
        pid, cust, why = row.project_id, project.customer_id, row.paused_reason
        out = view(s, row)
    audit.log(
        actor=actor or "system",
        action="automation.pause",
        target=f"project:{pid}",
        detail=f"{'automatic ' if automatic else ''}{why}",
        customer_id=cust,
    )
    return out


def resume(project_id: int, *, actor: str = "", now: datetime | None = None) -> dict:
    now = now or _now()
    with Session(engine) as s:
        project = _project(s, project_id)
        row = get(s, project.id)
        if row is None:
            raise AutomationError("This project has no content source.", 404)
        row.paused_at, row.paused_reason = None, ""
        row.consecutive_failures = 0
        if row.enabled:
            row.next_at = automation_schedule.next_fire(
                row.times, row.days, row.interval_minutes, now
            )
        row.updated_at = now
        s.add(row)
        s.commit()
        s.refresh(row)
        pid, cust = row.project_id, project.customer_id
        out = view(s, row)
    audit.log(
        actor=actor or "system",
        action="automation.resume",
        target=f"project:{pid}",
        detail="resumed",
        customer_id=cust,
    )
    return out


# ---------------------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------------------
def preflight(session: Session, row: ProjectAutomation, *, respect_pause: bool = False) -> str:
    """Why a run must not start now ("" = go). No egress. A lane first follows the picked
    connection (credential, posture, health) so a reconnect of the picked connection reaches it."""
    project = session.get(Project, row.project_id)
    if project is None:
        return "The project no longer exists."
    if not module_enabled():
        return "Project automation is switched off on this install."
    if getattr(project, "archived_at", None) is not None:
        return f"The project '{project.name}' is archived."
    if respect_pause and row.paused_at is not None:
        return "The automation is paused."
    if row.run_connection_id != row.connection_id:
        _sync_credential_to_lane(row.connection_id, row.run_connection_id, list(row.scope or {}))
    conn = session.get(Connection, row.run_connection_id, populate_existing=True)
    if conn is None:
        return "The connection no longer exists."
    if not registry.kind_enabled(conn.kind):
        return f"{conn.kind} connections are switched off on this install."
    if not access.org_active(getattr(project, "org_id", None)):
        return "The organization is suspended."
    if conn.status == ConnStatus.REAUTH:
        return "The connection needs to be reconnected."
    from app.services import project_budgets

    allowed, _remaining, msg = project_budgets.project_check(project.id)
    if not allowed:
        return msg or project_budgets.EXHAUSTED_MESSAGE
    return ""


def missed_slots(times, days, interval_minutes, first: datetime, now: datetime) -> int:
    """How many scheduled slots fell in [first, now] (at least 1 when `first <= now`)."""
    n, cur = 0, first
    while cur is not None and cur <= now and n < _MAX_MISSED_COUNT:
        n += 1
        cur = automation_schedule.next_fire(times, days, interval_minutes, cur)
    return n


def _payload(row: ProjectAutomation, *, trigger: str, mode: str, actor: str) -> dict:
    p = {
        "connection_id": row.run_connection_id,
        "actor": actor,
        "automation_id": row.id,
        "project_id": row.project_id,
        "trigger": trigger,
        "mode": mode,
    }
    if row.run_cap_usd:
        p["run_cap_usd"] = row.run_cap_usd
    return p


def run_now(
    project_id: int,
    *,
    actor: str = "",
    mode: str | None = None,
    trigger: str = "manual",
) -> tuple[int, bool]:
    """Start a run for the project now. Returns (job_id, created); created False = a run was
    already in flight and that job is handed back. Raises AutomationError."""
    _require_enabled()
    with Session(engine) as s:
        project = _project(s, project_id)
        row = get(s, project.id)
        if row is None:
            raise AutomationError(
                "This project has no content source. Bind a connection first.", 404
            )
        if row.paused_at is not None and trigger != "manual":
            raise AutomationError("The automation is paused.", 409)
        why = preflight(s, row)
        if why:
            raise AutomationError(f"Run not started: {why}", 409)
        m = (mode or row.mode or "delta").strip().lower()
        if m not in MODES:
            raise AutomationError("mode must be 'delta' or 'full'.", 400)
        conn = s.get(Connection, row.run_connection_id)
        if m == "full" and conn is not None and conn.kind in NON_FLOW_KINDS:
            raise AutomationError(f"A full pull is not available for {conn.kind} connections.", 400)
        payload = _payload(row, trigger=trigger, mode=m, actor=actor or "system")
        rid, aid, pid, cust = row.run_connection_id, row.id, row.project_id, project.customer_id
    jid, created = jobs.submit_unique("connector_translate", payload, actor=actor or "system")
    if created:
        _record_submit(aid, jid, now=_now())
        try:
            audit.log(
                actor=actor or "system",
                action="automation.run",
                target=f"project:{pid}",
                detail=f"job:{jid} trigger:{trigger} mode:{m} connection:{rid}",
                customer_id=cust,
            )
        except Exception:
            log.exception("Audit log failed for automation run (job %s)", jid)
    return jid, created


def _record_submit(
    aid: int, jid: int | None, *, now: datetime, status: str | None = None, message: str = ""
) -> None:
    """Remember the submitted job. A queued job's status is NOT written here (the worker may
    already have finished and settled it: this write would overwrite the real outcome); the
    page shows an in-flight job from the job itself. Only a skip records a status."""
    with Session(engine) as s:
        row = s.get(ProjectAutomation, aid)
        if row is None:
            return
        row.last_at = now
        if jid is not None:
            row.last_job_id = jid
        if status is not None:
            row.last_status = status
            row.last_message = (message or "")[:300]
        s.add(row)
        s.commit()


def _record_skip(aid: int, why: str, *, now: datetime) -> None:
    """A slot that could not run (budget used up, archived, kind off, reconnect needed). Writes
    the skipped state, and, once per CHANGE of reason (not every slot), an audit row and, for a
    used-up budget, a notification, so a 15-minute schedule does not log or post forever."""
    changed = False
    pid = cust = None
    with Session(engine) as s:
        row = s.get(ProjectAutomation, aid)
        if row is None:
            return
        pid = row.project_id
        changed = not (row.last_status == "skipped" and row.last_message == why[:300])
        row.last_at = now
        row.last_status = "skipped"
        row.last_message = why[:300]
        s.add(row)
        proj = s.get(Project, pid)
        cust = proj.customer_id if proj else None
        if changed and "budget" in why.lower():
            # one message per transition INTO this state; the time keeps a later, separate
            # exhaustion from being taken for a duplicate of this one
            _notify(
                "run.limit_hit",
                proj,
                {"limit": "budget", "dedup": f"skip-{aid}-{_sha(why)}-{now:%Y%m%dT%H%M%S}"},
                s,
            )
        s.commit()
    if changed:
        try:
            audit.log(
                actor="scheduler",
                action="automation.skipped",
                target=f"project:{pid}",
                detail=why[:200],
                customer_id=cust,
            )
        except Exception:
            log.exception("Audit log failed for skipped automation %s", aid)


def _notify(event: str, project, payload: dict, session: Session | None = None) -> None:
    """Hand an event to the notification system (never raises). With `session` the queued rows
    join that transaction and commit with the caller's write."""
    try:
        from app.services import notify

        notify.notify(event, project, payload, session=session)
    except Exception:
        log.exception("notify(%s) failed", event)


def tick(now: datetime | None = None) -> list[int]:
    """Fire every enabled, unpaused automation whose slot is due. Called from scheduler.tick
    BELOW the sync_scheduler_enabled kill switch. Returns the job ids it enqueued."""
    now = now or _now()
    fired: list[int] = []
    if not module_enabled():
        return fired
    with Session(engine) as s:
        candidates = [
            a.id
            for a in s.exec(
                select(ProjectAutomation).where(
                    ProjectAutomation.enabled == True,  # noqa: E712
                    ProjectAutomation.paused_at == None,  # noqa: E711
                )
            ).all()
        ]
    for aid in candidates:
        with Session(engine) as s:
            row = s.get(ProjectAutomation, aid)  # fresh read: honour an edit made mid-tick
            if row is None or not row.enabled or row.paused_at is not None:
                continue
            times, days, iv = row.times, row.days, row.interval_minutes
            if not automation_schedule.has_schedule(times, days, iv):
                continue
            if row.next_at is None:
                row.next_at = automation_schedule.next_fire(times, days, iv, now)
                s.add(row)
                s.commit()
                continue
            if row.next_at > now:
                continue
            # (1) consume the slot FIRST, atomically (UPDATE ... WHERE next_at = the value we
            # read): of two processes that both see the slot due only one wins it. A crash
            # after this skips one run, never doubles one.
            old_next = row.next_at
            new_next = automation_schedule.next_fire(times, days, iv, now)
            won = s.exec(
                update(ProjectAutomation)
                .where(ProjectAutomation.id == aid, ProjectAutomation.next_at == old_next)
                .values(next_at=new_next, last_at=now)
            )
            s.commit()
            if not won.rowcount:
                continue
            s.refresh(row)
            why = preflight(s, row, respect_pause=True)
            mode = row.mode
            # Slots missed during downtime run ONCE: this single run covers everything missed
            # (a delta pull reads every change since the last run; a full one reads it all),
            # and the next slot was just computed from NOW, so no second run follows.
            catch_up = now - old_next > CATCH_UP_GRACE
            missed = missed_slots(times, days, iv, old_next, now) if catch_up else 0
            trigger = "catch_up" if catch_up else "schedule"
            payload = _payload(row, trigger=trigger, mode=mode, actor="scheduler")
            if catch_up:
                payload["missed_slots"] = missed
            pid, rid = row.project_id, row.run_connection_id
            cust = None
            proj = s.get(Project, pid)
            if proj is not None:
                cust = proj.customer_id
        if why:
            _record_skip(aid, why, now=now)
            _after_failure_check(aid, skipped_reason=why)
            continue
        jid, created = jobs.submit_unique("connector_translate", payload, actor="scheduler")
        if not created:
            _record_submit(
                aid, jid, now=now, status="skipped", message="A run was still in flight."
            )
            continue
        _record_submit(aid, jid, now=now)
        try:
            audit.log(
                actor="scheduler",
                action="automation.run",
                target=f"project:{pid}",
                detail=f"job:{jid} trigger:{trigger} mode:{mode} connection:{rid}"
                + (f" missed_slots:{missed}" if catch_up else ""),
                customer_id=cust,
            )
        except Exception:
            log.exception("Audit log failed for scheduled automation run (job %s)", jid)
        fired.append(jid)
    return fired


def _after_failure_check(aid: int, *, skipped_reason: str) -> None:
    """A skipped slot that says the connection needs a reconnect pauses the automation."""
    if "reconnect" in skipped_reason.lower():
        try:
            with Session(engine) as s:
                row = s.get(ProjectAutomation, aid)
                pid = row.project_id if row else None
            if pid is not None:
                pause(pid, reason=skipped_reason, actor="scheduler", automatic=True)
        except AutomationError:
            pass


# ---------------------------------------------------------------------------------------
# claims (write-back ownership of external items when one connection feeds many projects)
# ---------------------------------------------------------------------------------------
def _owner_alive(s: Session, feeder_id: int, owner_pid: int) -> bool:
    """A claim is only worth honouring while its owner can still write: the project exists, is
    not archived, and is still bound (an automation row) or is the picked connection's own
    project."""
    owner = s.get(Project, owner_pid)
    if owner is None or getattr(owner, "archived_at", None) is not None:
        return False
    if s.exec(
        select(ProjectAutomation.id).where(ProjectAutomation.project_id == owner_pid)
    ).first():
        return True
    feeder = s.get(Connection, feeder_id)
    return bool(feeder is not None and feeder.project_id == owner_pid)


class ClaimTaker:
    """run_ctx claim object: the first project to reach an external (item, locale) owns its
    write-back. Two projects that translate the SAME item into DIFFERENT locales never block
    each other. A claim whose owner is archived or unbound is taken over (audited)."""

    def __init__(self, connection_id: int, project_id: int):
        self.connection_id = connection_id
        self.project_id = project_id
        self._seen: dict[tuple[str, str], bool] = {}
        self.owners: dict[int, int] = {}  # owner project id -> pairs refused in this run
        self.refused: dict[tuple[str, str], int] = {}  # refused pair -> its owner project
        self.taken_over = 0
        # The connection's claims read ONCE per run (pair -> owner project) and each other
        # owner's liveness once, so a run that already owns its pairs does not open a database
        # session per (item, locale). Only an unclaimed pair or a dead owner's pair goes to the
        # database (insert, or take over), where the unique key settles a race.
        self._known: dict[tuple[str, str], int] | None = None
        self._alive: dict[int, bool] = {}

    def _load(self) -> dict[tuple[str, str], int]:
        if self._known is None:
            with Session(engine) as s:
                # behind the claim gate (see hand_over): never read claims mid hand-over
                _claim_gate(s, (self.project_id,))
                self._known = {
                    (ext, loc): owner
                    for ext, loc, owner in s.exec(
                        select(
                            ProjectBindingClaim.external_id,
                            ProjectBindingClaim.locale,
                            ProjectBindingClaim.project_id,
                        ).where(ProjectBindingClaim.connection_id == self.connection_id)
                    ).all()
                }
                s.commit()
        return self._known

    def _owner_is_alive(self, owner_pid: int) -> bool:
        if owner_pid not in self._alive:
            with Session(engine) as s:
                self._alive[owner_pid] = _owner_alive(s, self.connection_id, owner_pid)
        return self._alive[owner_pid]

    def take(self, item_id, locale: str = "") -> bool:
        key = (str(item_id)[:500], str(locale or "")[:40])
        if key in self._seen:
            return self._seen[key]
        owner = self._load().get(key)
        if owner == self.project_id:
            mine = True
        elif owner is not None and self._owner_is_alive(owner):
            self.owners[owner] = self.owners.get(owner, 0) + 1
            self.refused[key] = owner
            mine = False
        else:
            mine = self._take(*key)
        self._seen[key] = mine
        return mine

    @property
    def mine(self) -> set:
        return {k for k, v in self._seen.items() if v}

    def _take(self, key: str, locale: str) -> bool:
        for _ in range(2):
            with Session(engine) as s:
                row = s.exec(
                    select(ProjectBindingClaim).where(
                        ProjectBindingClaim.connection_id == self.connection_id,
                        ProjectBindingClaim.external_id == key,
                        ProjectBindingClaim.locale == locale,
                    )
                ).first()
                if row is not None:
                    if row.project_id == self.project_id:
                        return True
                    if not _owner_alive(s, self.connection_id, row.project_id):
                        row.project_id = self.project_id
                        row.wanted_by = None
                        s.add(row)
                        s.commit()
                        self.taken_over += 1
                        return True
                    self.owners[row.project_id] = self.owners.get(row.project_id, 0) + 1
                    self.refused[(key, locale)] = row.project_id
                    return False
                s.add(
                    ProjectBindingClaim(
                        connection_id=self.connection_id,
                        external_id=key,
                        locale=locale,
                        project_id=self.project_id,
                    )
                )
                try:
                    s.commit()
                    return True
                except IntegrityError:
                    s.rollback()  # another project inserted it first: re-read the owner
        return False

    def mark_wanted(self, *, complete: bool) -> None:
        """Remember, on each refused pair's claim, that THIS project wanted it (so a hand-over
        from the owner can give exactly those pairs). After a complete listing a pair this
        project no longer asks for is cleared again. Never raises into the run."""
        try:
            with Session(engine) as s:
                rows = s.exec(
                    select(ProjectBindingClaim).where(
                        ProjectBindingClaim.connection_id == self.connection_id,
                        (ProjectBindingClaim.wanted_by == self.project_id)
                        | (ProjectBindingClaim.project_id.in_(set(self.refused.values()) or {-1})),
                    )
                ).all()
                for c in rows:
                    key = (c.external_id, c.locale)
                    if key in self.refused and c.project_id == self.refused[key]:
                        if c.wanted_by != self.project_id:
                            c.wanted_by = self.project_id
                            s.add(c)
                    elif complete and c.wanted_by == self.project_id:
                        c.wanted_by = None
                        s.add(c)
                s.commit()
        except Exception:
            log.exception("Could not record the wanted claims of project %s", self.project_id)

    def release_unseen(self) -> int:
        """After a complete listing: drop this project's claims on pairs the run did not reach
        any more (the item left its scope or was deleted upstream)."""
        mine = self.mine
        if not mine:
            return 0
        n = 0
        with Session(engine) as s:
            for c in s.exec(
                select(ProjectBindingClaim).where(
                    ProjectBindingClaim.connection_id == self.connection_id,
                    ProjectBindingClaim.project_id == self.project_id,
                )
            ).all():
                if (c.external_id, c.locale) not in mine:
                    s.delete(c)
                    n += 1
            s.commit()
        return n


def implicit_claimer(conn) -> ClaimTaker | None:
    """The claim object for a run that is NOT an automation job (a webhook, Translate now, the
    connection's own schedule) on a connection other projects hold claims on, so those paths
    cannot write over a claimed item either. None (the run is untouched) when no claim exists."""
    if conn is None or conn.project_id is None:
        return None
    feeder = (conn.config or {}).get("lane_of") or conn.id
    with Session(engine) as s:
        if (
            s.exec(
                select(ProjectBindingClaim.id).where(ProjectBindingClaim.connection_id == feeder)
            ).first()
            is None
        ):
            return None
    return ClaimTaker(feeder, conn.project_id)


def release_claims(project_id: int, *, reason: str = "") -> int:
    """Drop every claim a project holds (it was archived, unbound or removed)."""
    with Session(engine) as s:
        rows = s.exec(
            select(ProjectBindingClaim).where(ProjectBindingClaim.project_id == project_id)
        ).all()
        for r in rows:
            s.delete(r)
        s.commit()
    if rows:
        try:
            audit.log(
                actor="system",
                action="automation.claims_released",
                target=f"project:{project_id}",
                detail=f"{len(rows)} claim(s) {reason}".strip(),
            )
        except Exception:
            log.exception("Audit log failed for claim release of project %s", project_id)
    return len(rows)


# ---------------------------------------------------------------------------------------
# languages (connected projects, 2026-10-13 decision 4)
# ---------------------------------------------------------------------------------------
# A project with a content source decides its own languages. The run connection follows the
# project's ACTIVE list at run time (store_common.effective_locales); these hooks do the rest of
# the bookkeeping when a language is added or stopped. Stopping never deletes anything: segments,
# TM and what was written to the external system stay. The project's claims on that language are
# released (another project may take it) and the per-pair state that would make a later re-add
# skip its pairs is forgotten.
def _locale_key(locale) -> str:
    from app.services import languages_catalog

    return languages_catalog.normalize(str(locale or ""))


def _key_names_locale(key, want: str) -> bool:
    """True when a hash / fingerprint key is for the locale `want` (a normalized code): keys
    end in ':<locale>' (flow kinds, jira, git) or in a column name '... (<locale>)'."""
    text = str(key)
    tail = text.rsplit(":", 1)[-1]
    if _locale_key(tail) == want:
        return True
    if text.endswith(")") and "(" in text:
        return _locale_key(text[text.rindex("(") + 1 : -1]) == want
    return False


def forget_locale_state(connection_id: int, locale: str) -> int:
    """Drop the per-(record, locale) content hashes and read-back fingerprints of one language
    from a run connection's stored state, so the language's pairs are processed again when it
    comes back instead of being skipped as unchanged. Returns the entries dropped."""
    conn = conn_store.get(connection_id)
    if conn is None:
        return 0
    want = _locale_key(locale)
    updates: dict = {}
    dropped = 0
    for key, val in (conn.config or {}).items():
        if not isinstance(val, dict) or not str(key).endswith(("_hashes", "_target_prints")):
            continue
        keep = {k: v for k, v in val.items() if not _key_names_locale(k, want)}
        if len(keep) != len(val):
            dropped += len(val) - len(keep)
            updates[key] = keep
    if updates:
        conn_store.patch_config(connection_id, updates)
    return dropped


def release_locale_claims(project_id: int, locale: str) -> int:
    """Drop the claims a project holds on one language (and its 'wanted' marks on other
    projects' claims), so another project may take that language."""
    want = _locale_key(locale)
    n = 0
    with Session(engine) as s:
        for c in s.exec(
            select(ProjectBindingClaim).where(
                (ProjectBindingClaim.project_id == project_id)
                | (ProjectBindingClaim.wanted_by == project_id)
            )
        ).all():
            if _locale_key(c.locale) != want:
                continue
            if c.project_id == project_id:
                s.delete(c)
                n += 1
            else:
                c.wanted_by = None
                s.add(c)
        s.commit()
    return n


def sync_run_locales(project_id: int, *, actor: str = "") -> None:
    """Make the run connection's own target-locale settings follow the project's active
    languages (the run itself already reads the project; this keeps the connection screens and
    the single-locale push / pull in step). Never raises into the caller.

    Writes with the connection-side language hook switched off: this is the project telling the
    connection, not a person changing the connection's languages."""
    try:
        with Session(engine) as s:
            row = get(s, project_id)
            if row is None:
                return
            run_id = row.run_connection_id
            active = dedupe_locales(
                tl.locale
                for tl in s.exec(
                    select(TargetLocale)
                    .where(TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_(None))
                    .order_by(TargetLocale.id)
                ).all()
            )
        conn = conn_store.get(run_id)
        if conn is None or not active:
            return
        cfg = conn.config or {}
        # a language the connection already names keeps the connection's spelling (its wire
        # format: Akeneo fr_FR, Zendesk pt-br); the project's spelling only for a new one
        active = in_connection_spelling(active, config_locales(cfg, default=""))
        updates: dict = {"target_locales": ", ".join(active)}
        drop = ["language_set_id"] if cfg.get("language_set_id") else []
        keys = {_locale_key(l) for l in active}  # noqa: E741
        explicit = str(cfg.get("target_locale") or "").strip()
        if explicit and _locale_key(explicit) not in keys:
            # the single-locale round trip must never use a stopped language
            updates["target_locale"] = active[0]
        if updates != {k: cfg.get(k) for k in updates} or drop:
            with connection_follows_project():
                conn_store.patch_config(run_id, updates, remove=drop, actor=actor)
    except Exception:
        log.exception("Could not sync the run connection's languages for project %s", project_id)


# ---------------------------------------------------------------------------------------
# connection-side language edits (the project stays the source of truth)
# ---------------------------------------------------------------------------------------
_FOLLOWING_PROJECT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "project_automation_following_project", default=False
)


@contextlib.contextmanager
def connection_follows_project():
    """While open, a write to a connection's language settings is the PROJECT (or a bind) telling
    the connection, so `connection_language_plan` leaves it alone (no loop back into the
    project)."""
    token = _FOLLOWING_PROJECT.set(True)
    try:
        yield
    finally:
        _FOLLOWING_PROJECT.reset(token)


def _project_label(session: Session, project_id) -> str:
    project = session.get(Project, project_id)
    return f'"{project.name}" (#{project_id})' if project is not None else f"#{project_id}"


def connection_language_plan(
    connection_id, old_cfg: dict | None, new_cfg: dict | None, *, can_add: bool = True
) -> dict | None:
    """What a change of a connection's OWN language settings (target_locales, target_locale,
    language_set_id) means for the project(s) it serves, decided BEFORE the change is saved.

    Connected projects, decision 4: the project's language list is the only source of truth. So:
    - a plain connection (no project automation uses it), or no language change: None (save as
      today);
    - the run connection of a project (bound 1:1, the adopted case, or that project's own copy):
      the change flows into the PROJECT through the same add / stop service as the project page.
      A language the new list names and the project does not run is added (a stopped one comes
      back); a language the person removed from the connection is STOPPED (never deleted).
      Afterwards the connection's languages are rewritten to the project's list. An empty new
      list changes nothing on the project. Returns {"project_id", "add", "stop"};
    - a connection that only FEEDS other projects (each through its own copy, a lane): refused
      (AutomationError 409) naming those projects. Their languages are theirs; a change here
      would change nothing they run, so it is refused rather than saved silently.
    Refusals before anything is written: a code the language catalog does not know (400), an
    add by a caller who may not add a language to a connected project (`can_add` False: decision
    8, manager or higher; 403), and a change that would leave the project with no language (400).
    """
    if _FOLLOWING_PROJECT.get():
        return None
    old_cfg, new_cfg = dict(old_cfg or {}), dict(new_cfg or {})
    if all(old_cfg.get(k) == new_cfg.get(k) for k in _LANGUAGE_KEYS):
        return None
    old_l = config_locales(old_cfg, default="")
    new_l = dedupe_locales(config_locales(new_cfg, default=""))
    old_keys = {_locale_key(l) for l in old_l}  # noqa: E741
    new_keys = {_locale_key(l) for l in new_l}  # noqa: E741
    if old_keys == new_keys:
        return None
    cid = ids.coerce_id(connection_id)
    if cid is None:
        return None
    with Session(engine) as s:
        run_row = s.exec(
            select(ProjectAutomation).where(ProjectAutomation.run_connection_id == cid)
        ).first()
        if run_row is None:
            feeds = s.exec(
                select(ProjectAutomation).where(ProjectAutomation.connection_id == cid)
            ).all()
            if not feeds:
                return None
            names = ", ".join(_project_label(s, r.project_id) for r in feeds[:5])
            more = f" and {len(feeds) - 5} more" if len(feeds) > 5 else ""
            raise AutomationError(
                f"The languages of this connection are managed by project {names}{more}: each "
                "project that uses it keeps its own language list. Change the languages on the "
                "project instead.",
                409,
            )
        pid = run_row.project_id
        project = s.get(Project, pid)
        rows = s.exec(
            select(TargetLocale).where(TargetLocale.project_id == pid).order_by(TargetLocale.id)
        ).all()
        label = _project_label(s, pid)
    plan = {"project_id": pid, "add": [], "stop": []}
    if not new_l:
        return plan  # nothing named: the project keeps its languages, the connection follows
    active_keys = {_locale_key(r.locale) for r in rows if r.stopped_at is None}
    source_key = _locale_key(getattr(project, "source_locale", "") or "")
    add = [l for l in new_l if _locale_key(l) not in active_keys and _locale_key(l) != source_key]  # noqa: E741
    stop_keys = (old_keys - new_keys) & active_keys
    stop = dedupe_locales(
        r.locale for r in rows if r.stopped_at is None and _locale_key(r.locale) in stop_keys
    )
    if add:
        from app.services import languages_catalog

        _valid, invalid = languages_catalog.validate_codes(add)
        if invalid:
            raise AutomationError(
                f"Unknown language code(s): {', '.join(invalid)} (not in the language catalog). "
                f"The languages of this connection are the languages of project {label}.",
                400,
            )
        if not can_add:
            from app.services import connected_projects

            raise AutomationError(
                f"{connected_projects.LOCALE_ADD_MANAGER_ONLY} The languages of this connection "
                f"are the languages of project {label}.",
                403,
            )
    if not add and stop and not (active_keys - {_locale_key(l) for l in stop}):  # noqa: E741
        raise AutomationError(
            f"Project {label} must keep at least one language: this change would stop all of "
            "them. Add the new language first, or change the languages on the project.",
            400,
        )
    plan["add"], plan["stop"] = add, stop
    return plan


def apply_connection_language_plan(plan: dict | None, *, actor: str = "") -> dict | None:
    """Carry out `connection_language_plan` AFTER the connection change is saved: adds through
    content.add_target_locale, stops through content.remove_target_locale (a bound project
    stops, never deletes), then the connection's languages follow the project. Returns the plan
    with what actually happened ("added", "stopped"), or None for no plan."""
    if not plan:
        return None
    from app.services import content

    pid = plan["project_id"]
    added, stopped = [], []
    with Session(engine) as s:
        for loc in plan.get("add") or ():
            try:
                if content.add_target_locale(s, pid, loc, actor=actor):
                    added.append(loc)
            except Exception:
                log.exception("Could not add language %r to project %s", loc, pid)
        for loc in plan.get("stop") or ():
            try:
                if content.remove_target_locale(s, pid, loc, actor=actor) is not None:
                    stopped.append(loc)
            except Exception:
                log.exception("Could not stop language %r on project %s", loc, pid)
    sync_run_locales(pid, actor=actor)
    if added or stopped:
        with Session(engine) as s:
            project = s.get(Project, pid)
            cust = project.customer_id if project else None
        audit.log(
            actor=actor or "system",
            action="automation.languages_from_connection",
            target=f"project:{pid}",
            detail=(
                f"connection language edit: added {', '.join(added) or 'none'}; "
                f"stopped {', '.join(stopped) or 'none'}"
            ),
            customer_id=cust,
        )
    return {**plan, "added": added, "stopped": stopped}


def locale_added(project_id: int, locale: str, *, actor: str = "", restarted: bool = False) -> bool:
    """A language was added to (or brought back on) a project. A no-op for a project with no
    content source. For a bound one the next run includes the language (new pairs only), the
    run connection follows, and the change is audited. Returns True when the project is bound."""
    with Session(engine) as s:
        row = get(s, project_id)
        if row is None:
            return False
        run_id = row.run_connection_id
        project = s.get(Project, project_id)
        cust = project.customer_id if project else None
    if restarted:
        # a pair processed before the language was stopped must not look "unchanged" now
        forget_locale_state(run_id, locale)
    sync_run_locales(project_id, actor=actor)
    audit.log(
        actor=actor or "system",
        action="automation.locale_added",
        target=f"project:{project_id}",
        detail=f"{locale} {'restarted' if restarted else 'added'}; the next run includes it",
        customer_id=cust,
    )
    return True


def locale_stopped(project_id: int, locale: str, *, actor: str = "") -> bool:
    """A language of a bound project was stopped. Runs from now on do not pull, translate or
    write it; nothing already written is touched. Claims on it are released and the run
    connection follows the project. Returns True when the project is bound."""
    with Session(engine) as s:
        row = get(s, project_id)
        if row is None:
            return False
        run_id = row.run_connection_id
        project = s.get(Project, project_id)
        cust = project.customer_id if project else None
    released = release_locale_claims(project_id, locale)
    forget_locale_state(run_id, locale)
    sync_run_locales(project_id, actor=actor)
    audit.log(
        actor=actor or "system",
        action="automation.locale_stopped",
        target=f"project:{project_id}",
        detail=(f"{locale} stopped at the next run; nothing deleted; {released} claim(s) released"),
        customer_id=cust,
    )
    return True


# ---------------------------------------------------------------------------------------
# the job side
# ---------------------------------------------------------------------------------------
_UNSET = object()
MAX_CURSOR = 20000


def execute_job(payload: dict, progress, core) -> dict:
    """Run one automation `connector_translate` job: the lane sync and budget, the run cap (the
    smaller of the configured cap and what is left of the project's monthly budget), the full /
    claim switches, then `core(payload, progress)` (the ordinary connector run), then settle the
    automation's bookkeeping and notifications."""
    from app.services.jobs import ConnectorRunError, JobFailed, current_job_id

    aid = ids.coerce_id(payload.get("automation_id"))
    job_id = current_job_id()
    with Session(engine) as s:
        row = s.get(ProjectAutomation, aid) if aid is not None else None
        if row is None or row.run_connection_id != payload.get("connection_id"):
            msg = "Run skipped: the project's automation no longer uses this connection."
            progress(1, 1, msg)
            return {"message": msg, "ok": True, "skipped_automation_gone": True}
        feeder_id, lane_id, project_id = row.connection_id, row.run_connection_id, row.project_id
        mode = payload.get("mode") or row.mode or "delta"
        manual = payload.get("trigger") == "manual"
        conn = s.get(Connection, lane_id)
        kind = conn.kind if conn else ""
        why = preflight(s, row, respect_pause=not manual)
        cursor_start = list(row.full_cursor or [])
    if why:
        msg = f"Run skipped: {why}"
        _settle(aid, ok=True, skipped=True, message=msg, job_id=job_id)
        _after_failure_check(aid, skipped_reason=why)  # a reconnect-needed connection pauses
        progress(1, 1, msg)
        return {"message": msg, "ok": True, "skipped_automation": True}
    lane = lane_id != feeder_id
    # (run.started is posted by the job engine for every connector job: notify.on_connector_job)
    claims = None if kind in NON_FLOW_KINDS else ClaimTaker(feeder_id, project_id)
    full = mode == "full" and kind not in NON_FLOW_KINDS
    done = set(cursor_start) if full else None
    # the run's spend bound: the configured cap, and never more than the project's budget has left
    cap = float(payload.get("run_cap_usd") or 0)
    from app.services import project_budgets

    _allowed, remaining, _msg = project_budgets.project_check(project_id)
    budget_bound = False
    if remaining >= 0 and (cap <= 0 or remaining < cap):
        cap, budget_bound = max(remaining, 0.000001), True
    sink = None
    capped = False
    result = None
    try:
        with (
            spend.run_cap(cap) as sink,
            run_ctx.scope(full=full, claim_obj=claims, full_done=done),
        ):
            result = core(payload, progress)
    except JobFailed as exc:
        # Items the cap blocked come back as item failures: a run that stopped at its cap did
        # what it was told, so it is a (partial) success, not a failed run.
        if cap > 0 and sink is not None and sink.total >= cap:
            result = dict(getattr(exc, "result", None) or {})
            capped = True
        else:
            _settle(
                aid, ok=False, message=str(exc), result=getattr(exc, "result", None), job_id=job_id
            )
            raise
    except ConnectorRunError as exc:
        _settle(aid, ok=False, message=str(exc), job_id=job_id)
        raise
    except Exception as exc:
        _settle(aid, ok=False, message=f"{type(exc).__name__}: {exc}", job_id=job_id)
        raise
    finally:
        if lane:
            try:
                _sync_credential_from_lane(feeder_id, lane_id)
            except Exception:
                log.exception("Lane credential hand-back failed for connection %s", lane_id)
    out = dict(result or {})
    capped = capped or bool(cap > 0 and sink is not None and sink.total >= cap)
    if capped:
        out["ok"] = True
        out["spend_cap_hit"] = True
        out["limit"] = "budget" if budget_bound else "spend_cap"
        out["message"] = (
            f"{out.get('message') or 'Run stopped'} - stopped at the "
            f"{'project budget' if budget_bound else 'per-run spend cap'}; the rest waits for "
            "the next run."
        )[:300]
    elif out.get("ceiling_hit"):
        out["limit"] = "write_ceiling"
    cursor: object = _UNSET
    if full:
        # unfinished: remember what is done; finished: the next full run starts over
        stopped = capped or out.get("ceiling_hit")
        cursor = sorted(done or set())[:MAX_CURSOR] if stopped else []
    clean = bool(out.get("ok", True)) and not capped and not out.get("ceiling_hit")
    if claims is not None:
        if claims.taken_over:
            try:
                audit.log(
                    actor="system",
                    action="automation.claim_taken_over",
                    target=f"project:{project_id}",
                    detail=f"{claims.taken_over} pair(s) taken over from an archived/unbound owner",
                )
            except Exception:
                log.exception("Audit log failed for claim takeover of automation %s", aid)
        owners = dict(claims.owners)
        if owners:
            out["claim_owner_counts"] = {str(k): v for k, v in owners.items()}
        claims.mark_wanted(
            complete=bool(clean and not cursor_start and not out.get("listing_incomplete"))
        )
        written = int(out.get("created") or 0) + int(out.get("updated") or 0)
        if int(out.get("claim_conflicts") or 0) and not written:
            out["blocked_by_claims"] = True
            out["message"] = (
                f"Blocked: {out['claim_conflicts']} item(s) belong to another project's "
                "run on this connection, nothing was written here."
            )[:300]
        # Only a complete listing proves a pair left the scope: a run that could not read
        # every record (unreadable, refused for size, halted) keeps its claims.
        if clean and not cursor_start and not out.get("listing_incomplete"):
            try:
                claims.release_unseen()
            except Exception:
                log.exception("Claim release failed for automation %s", aid)
    out["automation_id"] = aid
    out["trigger"] = payload.get("trigger", "")
    out["mode"] = mode
    _settle(
        aid,
        ok=bool(out.get("ok", True)),
        message=str(out.get("message") or ""),
        result=out,
        job_id=job_id,
        cursor=cursor,
    )
    return out


def _counts(result: dict | None) -> dict:
    r = result or {}

    def n(key) -> int:
        v = r.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    created, updated = n("created"), n("updated")
    return {
        "created": created,
        "updated": updated,
        "written": created + updated,
        "held": n("held_back") + n("partial_held"),
        "failed": len(r.get("failures") or []),
    }


def _settle(
    aid: int,
    *,
    ok: bool,
    message: str = "",
    result: dict | None = None,
    skipped: bool = False,
    job_id: int | None = None,
    cursor=_UNSET,
) -> None:
    """Record the outcome, reset or bump the failure counter, auto-pause after N in a row, and
    queue the notifications in the same transaction. A run that finished with some item
    failures still counts as a success (partial); a run stopped by its cap or blocked by another
    project's claims is not a failure."""
    pause_reason = ""
    pid = cust = None
    conflicts = 0
    try:
        with Session(engine) as s:
            row = s.get(ProjectAutomation, aid)
            if row is None:
                return
            pid = row.project_id
            row.last_at = _now()
            row.last_message = (message or "")[:300]
            events: list[tuple[str, dict]] = []
            block_to: list[tuple[Project, dict]] = []
            counts = _counts(result)
            if skipped:
                row.last_status = "skipped"
            elif ok:
                row.last_status = "blocked" if (result or {}).get("blocked_by_claims") else "ok"
                row.consecutive_failures = 0
                limit = (result or {}).get("limit")
                if (result or {}).get("blocked_by_claims"):
                    block_to = _block_notices(s, row, result or {})
                else:
                    row.blocked_sig = ""
                if counts["written"] or counts["held"] or counts["failed"]:
                    events.append(("run.finished", {"counts": counts}))
                if counts["held"]:
                    events.append(("run.needs_review", {"counts": counts}))
                if limit:
                    events.append(("run.limit_hit", {"counts": counts, "limit": limit}))
            else:
                row.last_status = "failed"
                row.consecutive_failures = int(row.consecutive_failures or 0) + 1
                events.append(("run.failed", {"counts": counts}))
                conn = s.get(Connection, row.run_connection_id)
                if conn is not None and conn.status == ConnStatus.REAUTH:
                    pause_reason = "The connection needs to be reconnected."
                elif row.consecutive_failures >= int(row.auto_pause_after or 3):
                    pause_reason = (
                        f"Paused automatically after {row.consecutive_failures} failed runs "
                        "in a row."
                    )
            if cursor is not _UNSET:
                row.full_cursor = list(cursor) or None
            conflicts = int((result or {}).get("claim_conflicts") or 0)
            proj = s.get(Project, row.project_id)
            cust = proj.customer_id if proj else None
            s.add(row)
            for target, extra in block_to:
                _notify("automation.blocked", target, extra, s)
            for ev, extra in events:
                _notify(
                    ev,
                    proj,
                    # run_id "job<id>" is the key the job engine's own hook uses
                    # (notify.on_connector_job), so one message per channel and event
                    {**extra, "run_id": f"job{job_id}" if job_id else None, "at": _now()},
                    s,
                )
            s.commit()
        if conflicts:
            owners = (result or {}).get("claim_owner_counts") or {}
            audit.log(
                actor="system",
                action="automation.conflict",
                target=f"project:{pid}",
                detail=f"{conflicts} pair(s) owned by other project(s) {sorted(owners)} were "
                f"skipped (e.g. {','.join((result or {}).get('claim_conflict_ids', [])[:5])})",
                customer_id=cust,
            )
        if pause_reason:
            pause(pid, reason=pause_reason, actor="system", automatic=True)
    except Exception:
        log.exception("Could not settle project automation %s", aid)


def _block_notices(s: Session, row: ProjectAutomation, result: dict) -> list[tuple[Project, dict]]:
    """The messages for a run that was blocked by other projects' claims: to the blocked project
    and to each owning project, ONCE per change of the block (owner set or pair counts), not on
    every blocked run. Counts and project names only; another customer's project is never named
    (a connection with no customer can feed projects of several customers)."""
    owners = {
        int(k): int(v)
        for k, v in (result.get("claim_owner_counts") or {}).items()
        if str(k).isdigit() and isinstance(v, int)
    }
    if not owners:
        return []
    sig = _sha(",".join(f"{k}:{v}" for k, v in sorted(owners.items())))[:16]
    if row.blocked_sig == sig:
        return []
    row.blocked_sig = sig
    me = s.get(Project, row.project_id)
    if me is None:
        return []
    stamp = _now()
    out: list[tuple[Project, dict]] = []
    for opid, pairs in sorted(owners.items())[:5]:
        other = s.get(Project, opid)
        if other is None:
            continue
        same = other.customer_id == me.customer_id
        base = {"counts": {"pairs": pairs}, "at": stamp}
        key = f"blk-{me.id}-{opid}-{sig}-{stamp:%Y%m%dT%H%M%S}"
        out.append(
            (
                me,
                {
                    **base,
                    "role": "blocked",
                    "other": other.name if same else "",
                    "dedup": key + "-b",
                },
            )
        )
        out.append(
            (
                other,
                {
                    **base,
                    "role": "owner",
                    "other": me.name if same else "",
                    "dedup": key + "-o",
                },
            )
        )
    return out


# ---------------------------------------------------------------------------------------
# claim hand-over (the receiving project owns the pairs it was blocked on)
# ---------------------------------------------------------------------------------------
def handover_candidates(session: Session, project_id: int) -> dict:
    """What a hand-over could move for this project: `give` (pairs other projects wanted that
    this one owns: {receiver project id: pairs}) and `take` (pairs this project wanted that
    others own: {owner project id: pairs}). Plain ids and counts; callers name what they may."""
    give: dict[int, int] = {}
    take: dict[int, int] = {}
    for c in session.exec(
        select(ProjectBindingClaim).where(
            (ProjectBindingClaim.project_id == project_id)
            | (ProjectBindingClaim.wanted_by == project_id)
        )
    ).all():
        if c.project_id == project_id and c.wanted_by not in (None, project_id):
            give[c.wanted_by] = give.get(c.wanted_by, 0) + 1
        elif c.wanted_by == project_id and c.project_id != project_id:
            take[c.project_id] = take.get(c.project_id, 0) + 1
    return {"give": give, "take": take}


def _claim_gate(s: Session, project_ids) -> None:
    """Serialize claim readers and hand-overs per project: a no-op UPDATE of the project rows,
    held until the caller commits. On SQLite it takes the single writer lock (a second gate waits
    on busy_timeout), on Postgres the row locks. Must be the FIRST statement of the transaction
    (a SQLite read snapshot taken earlier could not be upgraded to a write)."""
    pids = sorted({int(p) for p in project_ids if p is not None})
    if pids:
        s.exec(update(Project).where(Project.id.in_(pids)).values(name=Project.name))


def _claim_writer_busy(s: Session, project_ids, feeder_id: int) -> bool:
    """True while any connector run that would write AS one of these projects on this
    connection family is queued or running: an automation run on the project's run connection,
    and also a webhook / Translate now / own-schedule run on a lane or picked connection that
    imports into the project (those take the project's claims too, see implicit_claimer)."""
    pids = {int(p) for p in project_ids}
    conn_ids: set[int] = set()
    for r in s.exec(
        select(ProjectAutomation).where(ProjectAutomation.project_id.in_(sorted(pids)))
    ).all():
        conn_ids.add(r.run_connection_id)
    for c in s.exec(select(Connection).where(Connection.project_id.in_(sorted(pids)))).all():
        if c.id == feeder_id or (c.config or {}).get("lane_of") == feeder_id:
            conn_ids.add(c.id)
    for j in s.exec(
        select(Job).where(
            Job.kind == "connector_translate",
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    ).all():
        if (j.payload or {}).get("connection_id") in conn_ids:
            return True
    return False


def hand_over(owner_id: int, receiver_id: int, *, actor: str = "") -> dict:
    """Give the receiving project ownership of the pairs it was blocked on (claims the owner
    holds that the receiver's last complete run was refused). The caller has already checked who
    may do it. Refused while either project has a run in flight (a run reads its claims once at
    the start, so moving them under it would let it write pairs it no longer owns). Audited as
    `automation.claim_handed_over`; the run history of both projects shows it. Raises
    AutomationError."""
    _require_enabled()
    oid, rid = ids.coerce_id(owner_id), ids.coerce_id(receiver_id)
    if oid is None or rid is None or oid == rid:
        raise AutomationError("Pick two different projects.", 400)
    with Session(engine) as s:
        # FIRST statement: take the claim gate of both projects (held to the commit). A run
        # reads its claims behind the same gate (ClaimTaker._load), so a run either loaded its
        # claims before this point (then it is in flight and refused below) or loads them after
        # the commit (then it sees the new owner). No run can act on the old owner afterwards.
        _claim_gate(s, (oid, rid))
        owner, receiver = _project(s, oid), _project(s, rid)
        r_row = get(s, rid)
        if r_row is None:
            raise AutomationError("The receiving project has no content source.", 409)
        if getattr(receiver, "archived_at", None) is not None:
            raise AutomationError("The receiving project is archived.", 409)
        claims = s.exec(
            select(ProjectBindingClaim).where(
                ProjectBindingClaim.project_id == oid,
                ProjectBindingClaim.wanted_by == rid,
                ProjectBindingClaim.connection_id == r_row.connection_id,
            )
        ).all()
        if not claims:
            raise AutomationError(
                "Nothing to hand over: the receiving project has no pairs waiting on this one.",
                409,
            )
        if _claim_writer_busy(s, (oid, rid), r_row.connection_id):
            raise AutomationError(
                "A run is in progress for one of the two projects. Hand over when it has finished.",
                409,
            )
        n = len(claims)
        for c in claims:
            c.project_id = rid
            c.wanted_by = None
            s.add(c)
        if r_row.blocked_sig:
            r_row.blocked_sig = ""
            s.add(r_row)
        s.commit()
        o_name, r_name, o_cust = owner.name, receiver.name, owner.customer_id
    audit.log(
        actor=actor or "system",
        action="automation.claim_handed_over",
        target=f"project:{oid}",
        detail=f"to_project:{rid} pairs:{n}",
        customer_id=o_cust,
        force=True,  # an ownership change is always recorded (and the run history reads it)
    )
    return {
        "ok": True,
        "from_project_id": oid,
        "to_project_id": rid,
        "pairs": n,
        "from_name": o_name,
        "to_name": r_name,
    }


# ---------------------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------------------
def view(
    session: Session,
    row: ProjectAutomation | None,
    *,
    can_see_cost: bool = True,
    visible=None,
) -> dict:
    """The automation as a plain dict for every surface. `visible(project_id) -> bool` limits
    which OTHER projects are named (a global connection can feed projects the caller cannot
    see: those are only counted); None names them all (the in-process operator)."""
    if row is None:
        return {"bound": False}
    conn = session.get(Connection, row.connection_id)
    run_conn = session.get(Connection, row.run_connection_id)
    project = session.get(Project, row.project_id)
    other = [
        a.project_id
        for a in session.exec(
            select(ProjectAutomation).where(
                ProjectAutomation.connection_id == row.connection_id,
                ProjectAutomation.project_id != row.project_id,
            )
        ).all()
    ]
    can = visible or (lambda _pid: True)
    shared_visible = [p for p in other if can(p)]
    owned: dict[int, int] = {}
    for c in session.exec(
        select(ProjectBindingClaim).where(
            ProjectBindingClaim.connection_id == row.connection_id,
            ProjectBindingClaim.project_id != row.project_id,
        )
    ).all():
        owned[c.project_id] = owned.get(c.project_id, 0) + 1
    owned_elsewhere = []
    for opid, n in sorted(owned.items()):
        if can(opid):
            oproj = session.get(Project, opid)
            owned_elsewhere.append(
                {"project_id": opid, "name": oproj.name if oproj else "", "pairs": n}
            )
    warnings = []
    if row.last_status == "blocked":
        who = ", ".join(f"{o['name'] or o['project_id']} ({o['pairs']})" for o in owned_elsewhere)
        warnings.append(
            "The last run was blocked: its items belong to another project on this connection"
            + (f": {who}." if who else ".")
            + " A manager of the owning project can hand the items over, or you can unbind "
            "or archive that project, or narrow one project's scope or languages."
        )
    if (
        conn is not None
        and conn.project_id not in (None, row.project_id)
        and (run_conn is not None and run_conn.id == conn.id)
    ):
        warnings.append("This connection also feeds another project outside automation.")
    if (
        conn is not None
        and project is not None
        and conn.project_id == row.project_id
        and run_conn is not None
        and run_conn.id != conn.id
    ):
        warnings.append("The picked connection runs on its own for this project too.")
    if run_conn is not None:
        src = (run_conn.config or {}).get("source_locale") or (run_conn.config or {}).get(
            "source_lang"
        )
        if src and project is not None and str(src).lower() != str(project.source_locale).lower():
            warnings.append(
                f"The connection reads {src} but the project's source language is "
                f"{project.source_locale}."
            )
    out = {
        "bound": True,
        "project_id": row.project_id,
        "connection_id": row.connection_id,
        "connection_name": conn.name if conn else "",
        "connection_kind": conn.kind if conn else "",
        "connection_status": run_conn.status if run_conn else "",
        "run_connection_id": row.run_connection_id,
        "own_copy": row.run_connection_id != row.connection_id,
        "shared_with_projects": shared_visible,
        "shared_with_hidden": len(other) - len(shared_visible),
        "owned_elsewhere": owned_elsewhere,
        "blocked": row.last_status == "blocked",
        "enabled": bool(row.enabled),
        "paused": row.paused_at is not None,
        "paused_reason": row.paused_reason or "",
        "mode": row.mode,
        "times": list(row.times or []),
        "days": list(row.days or []),
        "interval_minutes": int(row.interval_minutes or 0),
        "schedule": automation_schedule.describe(row.times, row.days, row.interval_minutes),
        "timezone": "UTC",
        "next_at": row.next_at.isoformat(timespec="seconds") + "Z" if row.next_at else None,
        "last_at": row.last_at.isoformat(timespec="seconds") + "Z" if row.last_at else None,
        "last_job_id": row.last_job_id,
        "last_status": row.last_status,
        "last_message": row.last_message,
        "consecutive_failures": int(row.consecutive_failures or 0),
        "auto_pause_after": int(row.auto_pause_after or DEFAULT_AUTO_PAUSE_AFTER),
        "run_cap_usd": row.run_cap_usd,
        "scope": dict(row.scope or {}),
        "full_pull_supported": bool(run_conn and run_conn.kind not in NON_FLOW_KINDS),
        "warnings": warnings,
    }
    # the languages the next run covers: the PROJECT's active list (connected projects)
    _tls = session.exec(
        select(TargetLocale)
        .where(TargetLocale.project_id == row.project_id)
        .order_by(TargetLocale.id)
    ).all()
    out["languages"] = [t.locale for t in _tls if t.stopped_at is None]
    out["stopped_languages"] = [t.locale for t in _tls if t.stopped_at is not None]
    cand = handover_candidates(session, row.project_id)

    def named(group: dict) -> list[dict]:
        res = []
        for opid, n in sorted(group.items()):
            if can(opid):
                other = session.get(Project, opid)
                res.append({"project_id": opid, "name": other.name if other else "", "pairs": n})
        return res

    # pairs this project owns that another project is waiting for / pairs this project is
    # waiting for: the one-click hand-over buttons (only projects the caller can see are named)
    out["handover_give"] = named(cand["give"])
    out["handover_take"] = named(cand["take"])
    if not can_see_cost:
        out["run_cap_usd"] = None
    return out


_COUNT_KEYS = ("created", "updated", "skipped", "held_back", "partial_held", "claim_conflicts")


def history(session: Session, project_id: int, *, limit: int = 20, can_see_cost: bool = False):
    """Recent automation runs for the project, newest first (Job rows tagged with this
    automation id). Cost figures only when `can_see_cost` (manager and up, ADR 0042)."""
    project = _project(session, project_id)
    row = get(session, project.id)
    if row is None:
        raise AutomationError("This project has no content source.", 404)
    limit = max(1, min(int(limit or 20), 100))
    out = []
    # The newest 2000 connector jobs are scanned for this automation's tag (payload JSON).
    for j in session.exec(
        select(Job).where(Job.kind == "connector_translate").order_by(Job.id.desc()).limit(2000)
    ).all():
        p = j.payload or {}
        if p.get("automation_id") != row.id:
            continue
        r = j.result or {}
        item = {
            "job_id": j.id,
            "trigger": p.get("trigger", ""),
            "mode": p.get("mode", ""),
            "status": j.status,
            "started_at": (j.started_at or j.created_at).isoformat(timespec="seconds") + "Z",
            "finished_at": j.finished_at.isoformat(timespec="seconds") + "Z"
            if j.finished_at
            else None,
            "message": (r.get("message") or j.error or j.message or "")[:300],
            "failed_items": len(r.get("failures") or []),
            "spend_cap_hit": bool(r.get("spend_cap_hit")),
            "blocked": bool(r.get("blocked_by_claims")),
            "limit": r.get("limit") or "",
            "missed_slots": int(p.get("missed_slots") or 0),
            "kind": "run",
        }
        for k in _COUNT_KEYS:
            item[k] = int(r.get(k) or 0) if isinstance(r.get(k), (int, float)) else 0
        if can_see_cost:
            item["cost_usd"] = j.cost_usd
        out.append(item)
        if len(out) >= limit:
            break
    handovers = _handover_history(session, project.id, limit)
    if can_see_cost:
        for h in handovers:
            h["cost_usd"] = None
    out.extend(handovers)
    out.sort(key=lambda i: (i["started_at"], i["kind"] == "handover"), reverse=True)
    return out[:limit]


def _handover_history(session: Session, project_id: int, limit: int) -> list[dict]:
    """Hand-overs this project took part in (as giver or receiver), as history rows."""
    from app.models import AuditLog

    rows = session.exec(
        select(AuditLog)
        .where(
            AuditLog.action == "automation.claim_handed_over",
            (AuditLog.target == f"project:{project_id}")
            | AuditLog.detail.like(f"to_project:{project_id} %"),
        )
        .order_by(AuditLog.id.desc())
        .limit(limit)
    ).all()
    out = []
    for a in rows:
        detail = a.detail or ""
        to_id = pairs = 0
        for part in detail.split():
            k, _, v = part.partition(":")
            if k == "to_project" and v.isdigit():
                to_id = int(v)
            elif k == "pairs" and v.isdigit():
                pairs = int(v)
        gave = a.target == f"project:{project_id}"
        other_id = to_id if gave else _target_id(a.target)
        other = session.get(Project, other_id) if other_id else None
        stamp = a.ts.isoformat(timespec="seconds") + "Z" if a.ts else ""
        out.append(
            {
                "kind": "handover",
                "job_id": None,
                "trigger": "handover",
                "mode": "",
                "status": "handed over",
                "started_at": stamp,
                "finished_at": stamp,
                "message": (
                    f"Handed {pairs} pair(s) over to project {other.name if other else other_id}"
                    if gave
                    else f"Took over {pairs} pair(s) from project "
                    f"{other.name if other else other_id}"
                )[:300],
                "failed_items": 0,
                "spend_cap_hit": False,
                "blocked": False,
                "limit": "",
                "missed_slots": 0,
                "by": a.actor,
                "other_project_id": other_id,
                "pairs": pairs,
                **dict.fromkeys(_COUNT_KEYS, 0),
            }
        )
    return out


def _target_id(target: str) -> int:
    tail = (target or "").rpartition(":")[2]
    return int(tail) if tail.isdigit() else 0


def active_job(session: Session, row: ProjectAutomation) -> int | None:
    for j in session.exec(
        select(Job).where(
            Job.kind == "connector_translate",
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    ).all():
        if (j.payload or {}).get("connection_id") == row.run_connection_id:
            return j.id
    return None


def global_switch_on() -> bool:
    return settings.get("sync_scheduler_enabled", "1") == "1"
