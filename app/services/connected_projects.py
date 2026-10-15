"""Connected projects: a third kind of project next to Documents and Keys.

A connected project takes its content from a connected system instead of uploads. It is created
FIRST, empty; the project page then offers a setup menu that picks (or establishes) the connection,
and that connection becomes the project's content. Creation and binding are two steps.

What lives here (and nothing else; the bind, schedule and run machinery stay in
`app/services/project_automation.py`, which this module only reads):

  * the stored marker `Project.intake` (`upload` | `connected`), separate from `project_type`
    (the content SHAPE that picks the editor), and the rule for changing it: only while the project
    has no documents, no keys and no automation binding;
  * the upload refusal every upload route funnels through (`refuse_upload`), so the API and the
    CLI cannot bypass the hidden page controls;
  * the read models for the project page (the setup menu / Source card), the Projects list badge,
    and the no-egress Preview of the first run;
  * `safe_return_to`, the one validator for the `return_to` that "Connect a new system" carries
    through the connector setup and back to the project.

Unbinding keeps a project connected (it shows the setup menu again). A template applied to a
connected project may pre-select a connection but never binds and never schedules.

Documented in: docs/architecture/project-automation.md
"""

from __future__ import annotations

import contextlib
import re

from sqlmodel import Session, func, select

from app.models import (
    Document,
    Project,
    ProjectAutomation,
    ProjectIntake,
    ProjectType,
    StringKey,
    TargetLocale,
)
from app.services import connectors as registry
from app.services.connectors import store as conn_store

#: Message every upload route answers a connected project with (HTTP 400).
UPLOAD_REFUSED = (
    "This is a connected project: its content comes from its connected system, so files "
    "cannot be uploaded to it. Run the project's source to pull content."
)


class IntakeError(ValueError):
    """A refused intake change or a refused action on a connected project (HTTP 400)."""

    status = 400


class InvalidIntakeError(IntakeError):
    """`intake` is not one of ProjectIntake.ALL."""

    def __init__(self, value):
        super().__init__(
            f"intake {value!r} is not available (expected one of: {', '.join(ProjectIntake.ALL)})."
        )


class UploadRefusedError(IntakeError):
    def __init__(self, message: str = UPLOAD_REFUSED):
        super().__init__(message)


def is_connected(project) -> bool:
    return getattr(project, "intake", ProjectIntake.UPLOAD) == ProjectIntake.CONNECTED


#: Adding a language to a project with a content source widens what its next run translates (and
#: spends), so it needs a manager. Plain and unbound ones keep the write role.
LOCALE_ADD_MANAGER_ONLY = (
    "Adding a language to a project with a connected source requires a manager role: it widens "
    "what the next run translates."
)


def is_bound(session: Session, project_id: int) -> bool:
    """True when the project has a content source (a ProjectAutomation binding)."""
    return (
        session.exec(
            select(ProjectAutomation.id).where(ProjectAutomation.project_id == project_id)
        ).first()
        is not None
    )


def refuse_upload(project) -> None:
    """Raise UploadRefusedError when `project` is connected. Called by every route that adds
    files or keys by hand (web, /api, CLI); the connector flows write through their own path and
    never call it."""
    if is_connected(project):
        raise UploadRefusedError()


def normalize_intake(value) -> str:
    """None / blank -> upload (what every existing caller passes implicitly). Raises
    ValueError subclass `InvalidIntakeError` for anything else not in ProjectIntake.ALL."""
    raw = ("" if value is None else str(value)).strip().lower()
    if not raw:
        return ProjectIntake.UPLOAD
    if raw not in ProjectIntake.ALL:
        raise InvalidIntakeError(value)
    return raw


def check_create(intake: str, project_type: str) -> None:
    """Rules for creating a connected project: the shape is `document` (a connector that
    delivers keys sets key_based at bind time, a later train), and the automation module must be
    on. Raises IntakeError (a clean 400 on every surface)."""
    if intake != ProjectIntake.CONNECTED:
        return
    if project_type != ProjectType.DOCUMENT:
        raise IntakeError(
            "A connected project is created as a Documents project; its shape follows the "
            "connected system. Leave the project type blank when you choose Connected."
        )
    from app.services import project_automation

    if not project_automation.module_enabled():
        raise IntakeError(
            "Connected projects are switched off on this install (setting "
            "project_automation_enabled=0)."
        )


def content_blockers(session: Session, project_id: int) -> list[str]:
    """Why a project's intake can no longer change ([] = it is empty)."""
    why = []
    n_docs = session.exec(
        select(func.count()).select_from(Document).where(Document.project_id == project_id)
    ).one()
    if n_docs:
        why.append("it has documents")
    n_keys = session.exec(
        select(func.count()).select_from(StringKey).where(StringKey.project_id == project_id)
    ).one()
    if n_keys:
        why.append("it has keys")
    bound = session.exec(
        select(ProjectAutomation.id).where(ProjectAutomation.project_id == project_id)
    ).first()
    if bound is not None:
        why.append("it is bound to a source")
    return why


def set_intake(session: Session, project: Project, intake, *, actor: str = "") -> Project:
    """Switch a project between upload and connected. Allowed only while the project has no
    documents, no keys and no automation binding; otherwise IntakeError with the reasons."""
    new = normalize_intake(intake)
    if new == (project.intake or ProjectIntake.UPLOAD):
        return project
    if new == ProjectIntake.CONNECTED:
        check_create(new, project.project_type)
    why = content_blockers(session, project.id)
    if why:
        raise IntakeError(
            "The intake of this project can no longer be changed: " + " and ".join(why) + ". "
            "Only an empty project can switch between uploads and a connected source."
        )
    project.intake = new
    session.add(project)
    session.commit()
    session.refresh(project)
    with contextlib.suppress(Exception):  # an audit failure never undoes the change
        from app.services import audit

        audit.log(
            actor=actor or "system",
            action="project.intake_set",
            target=f"project:{project.id}",
            detail=f"intake:{new}",
            customer_id=project.customer_id,
        )
    return project


# ---------------------------------------------------------------------------------------
# Shape on bind, first-pull adoption, "Make this a connected project"
# ---------------------------------------------------------------------------------------
#: Connector kinds whose content is strings (keys) in a key project. Every other kind delivers
#: documents. A connected project is created as `document`; binding one of these to a project
#: that is still EMPTY makes it `key_based`.
KEYS_KINDS = frozenset({"github", "gitlab", "bitbucket"})


def _content_counts(session: Session, project_id: int) -> tuple[int, int]:
    n_docs = session.exec(
        select(func.count()).select_from(Document).where(Document.project_id == project_id)
    ).one()
    n_keys = session.exec(
        select(func.count()).select_from(StringKey).where(StringKey.project_id == project_id)
    ).one()
    return int(n_docs), int(n_keys)


def shape_for_bind(
    session: Session, project: Project, kind: str, *, assume_connected: bool = False
) -> str | None:
    """The project_type a bind of a `kind` connection must apply to `project`, or None when the
    shape already fits. Only a CONNECTED project ever changes shape, and only while it is empty
    (no documents, no keys): the one exception to "project_type is immutable". A keys kind on a
    non-empty document project, or a document kind on a key project that holds keys, raises
    IntakeError with a clear message. Plain (upload) projects are never touched.
    `assume_connected` is for a project that is about to become connected in the same change."""
    if not (assume_connected or is_connected(project)):
        return None
    have = project.project_type or ProjectType.DOCUMENT
    want = ProjectType.KEY_BASED if kind in KEYS_KINDS else ProjectType.DOCUMENT
    if have == want:
        return None
    n_docs, n_keys = _content_counts(session, project.id)
    if want == ProjectType.KEY_BASED:
        if n_docs or n_keys:
            raise IntakeError(
                f"A {kind} connection delivers keys, but this connected project "
                f"already holds {n_docs} document(s). Only an empty connected project can take "
                "a keys source. Create a new connected project for it."
            )
        from app.services import keys as keys_svc

        if not keys_svc.enabled():
            raise IntakeError(
                f"A {kind} connection keeps its strings in a key project, and key projects are "
                "turned off on this install (setting key_projects_enabled)."
            )
        return want
    if n_keys or n_docs:
        raise IntakeError(
            f"A {kind} connection delivers documents, but this connected project is a keys "
            f"project that already holds {n_keys} key(s). Create a new connected project for it."
        )
    return want


def apply_shape(session: Session, project: Project, new_type: str | None) -> str | None:
    """Apply the result of `shape_for_bind` to the (uncommitted) project. Returns the old type
    when it changed, else None. The caller commits with its own change and then calls
    `audit_shape` (an audit write inside the open transaction would wait on the database)."""
    if not new_type or new_type == project.project_type:
        return None
    old = project.project_type
    project.project_type = new_type
    session.add(project)
    return old


def audit_shape(project_id, customer_id, old, new, *, kind: str = "", actor: str = "") -> None:
    """Audit row for a shape change made at bind time (after the commit)."""
    if not old or not new:
        return
    with contextlib.suppress(Exception):
        from app.services import audit

        audit.log(
            actor=actor or "system",
            action="project.type_set",
            target=f"project:{project_id}",
            detail=f"project_type:{old}->{new} (bound {kind or 'source'}, project was empty)",
            customer_id=customer_id,
        )


def adopt_new_project(session: Session, proj: Project, conn) -> bool:
    """First pull of a connection with no project (`store_part2._ensure_project`): make the
    project it just created a CONNECTED project bound to that same connection (no lane, nothing
    scheduled: `enabled` stays False). Languages are already the connection's target locales
    (the project was created from them). Returns False and leaves the project exactly as the
    legacy path made it when the automation module is off, another project already runs on
    this connection, or anything goes wrong: the pull itself must never fail because of this."""
    from app.services import project_automation

    try:
        if not project_automation.module_enabled():
            return False
        # Any automation already on this connection (run directly, or as the feeder of another
        # project's lane) keeps the legacy project: one connection never gains a second binding
        # by itself (kinds outside the flow engine take no per-item claims).
        if session.exec(
            select(ProjectAutomation.id).where(
                (ProjectAutomation.run_connection_id == conn.id)
                | (ProjectAutomation.connection_id == conn.id)
            )
        ).first():
            return False
        new_type = shape_for_bind(session, proj, conn.kind, assume_connected=True)
        proj.intake = ProjectIntake.CONNECTED
        old_type = apply_shape(session, proj, new_type)
        session.add(proj)
        session.add(
            ProjectAutomation(
                project_id=proj.id,
                org_id=proj.org_id,
                connection_id=conn.id,
                run_connection_id=conn.id,
                created_by=conn.created_by or "",
            )
        )
        session.commit()
        session.refresh(proj)
    except Exception:
        session.rollback()
        session.refresh(proj)
        return False
    audit_shape(
        proj.id, proj.customer_id, old_type, new_type, kind=conn.kind, actor=conn.created_by
    )
    with contextlib.suppress(Exception):
        from app.services import audit

        audit.log(
            actor=conn.created_by or "system",
            action="automation.bind",
            target=f"project:{proj.id}",
            detail=f"connection:{conn.id} run_connection:{conn.id} first-pull",
            customer_id=proj.customer_id,
        )
    return True


def _is_foreign_document(doc, conn) -> bool:
    """True when `doc` was uploaded rather than pulled by `conn` (a connector document carries
    fmt == the connector kind and, when stamped, the connection id in its skeleton)."""
    if (doc.fmt or "") != conn.kind:
        return True
    skel = doc.skeleton if isinstance(doc.skeleton, dict) else {}
    cid = skel.get("connector_id")
    return cid is not None and cid != conn.id


def convert_candidates(session: Session, project: Project) -> list:
    """The connections that feed `project` through Connection.project_id (lanes never count)."""
    from app.models import Connection
    from app.services.connectors.store_common_leaf import is_lane

    rows = session.exec(select(Connection).where(Connection.project_id == project.id)).all()
    return [c for c in rows if not is_lane(c)]


def convert_offer(session: Session, project: Project) -> list[dict]:
    """For the project page: the connections "Make this a connected project" could bind (empty
    list = do not show the button). A pure read; the real refusals (uploaded documents and so
    on) are raised by `convert_existing` with their explanation."""
    from app.services import project_automation

    if (
        is_connected(project)
        or getattr(project, "archived_at", None) is not None
        or getattr(project, "inbox_key", None)
        or not project_automation.module_enabled()
        or project_automation.get(session, project.id) is not None
    ):
        return []
    return [
        {"id": c.id, "name": c.name, "kind": c.kind}
        for c in convert_candidates(session, project)
        if project_automation.connection_visible_to_project(c, project)
    ]


def convert_existing(session: Session, project: Project, connection_id=None, *, actor: str = ""):
    """ "Make this a connected project" (manager and up, checked by the caller): an existing
    project that a connection already feeds through Connection.project_id, with no binding,
    becomes a connected project bound to THAT connection. No lane, no scope change, no schedule,
    no run, nothing written to the connection. Refused (IntakeError, 400, with the reason) for
    an archived or inbox project, one that is already connected or bound, one with documents
    that were uploaded rather than pulled by this connection, and a connection another project
    already runs on. Returns the new ProjectAutomation row. Never called by a migration or boot:
    an upgrade changes nothing on an existing install."""
    from sqlalchemy.exc import IntegrityError

    from app.services import ids, project_automation

    if not project_automation.module_enabled():
        raise IntakeError(
            "Connected projects are switched off on this install (setting "
            "project_automation_enabled=0)."
        )
    if getattr(project, "archived_at", None) is not None:
        raise IntakeError("An archived project cannot become a connected project.")
    if getattr(project, "inbox_key", None):
        raise IntakeError("An inbox project cannot become a connected project.")
    if is_connected(project):
        raise IntakeError(
            "This project is already a connected project. Connect its source from the project page."
        )
    if project_automation.get(session, project.id) is not None:
        raise IntakeError("This project already has a content source.")
    cands = convert_candidates(session, project)
    if not cands:
        raise IntakeError(
            "No connection feeds this project, so there is nothing to connect. Create a "
            "connected project instead and pick a source in its setup."
        )
    want = ids.coerce_id(connection_id) if connection_id not in (None, "") else None
    if connection_id not in (None, "") and want is None:
        raise IntakeError("That connection does not feed this project.")
    if want is not None:
        conn = next((c for c in cands if c.id == want), None)
        if conn is None:
            raise IntakeError("That connection does not feed this project.")
    elif len(cands) == 1:
        conn = cands[0]
    else:
        names = ", ".join(f"#{c.id} {c.name}" for c in cands[:6])
        raise IntakeError(
            f"More than one connection feeds this project ({names}). Say which one to connect."
        )
    if not project_automation.connection_visible_to_project(conn, project):
        raise IntakeError("That connection cannot be used by this project.")
    if session.exec(
        select(ProjectAutomation.id).where(ProjectAutomation.run_connection_id == conn.id)
    ).first():
        raise IntakeError("Another project already runs on this connection.")
    # Same rule as bind: a kind outside the flow engine takes no per-item claims, so a connection
    # that already feeds another project's own copy (lane) can serve only that one project.
    if (
        conn.kind in project_automation.NON_FLOW_KINDS
        and session.exec(
            select(ProjectAutomation.id).where(ProjectAutomation.connection_id == conn.id)
        ).first()
    ):
        raise IntakeError(
            f"Another project already uses this {conn.kind} connection, and {conn.kind} writes "
            "are not claimed per item, so one connection can serve only one project."
        )
    foreign = [
        d.id
        for d in session.exec(select(Document).where(Document.project_id == project.id)).all()
        if _is_foreign_document(d, conn)
    ]
    if foreign:
        raise IntakeError(
            f"This project has {len(foreign)} document(s) that were uploaded rather than pulled "
            f"from {conn.name}. A connected project takes its content only from its source, so "
            "it cannot be converted. Create a new connected project for the source instead."
        )
    new_type = shape_for_bind(session, project, conn.kind, assume_connected=True)
    row = ProjectAutomation(
        project_id=project.id,
        org_id=project.org_id,
        connection_id=conn.id,
        run_connection_id=conn.id,
        created_by=actor,
    )
    project.intake = ProjectIntake.CONNECTED
    old_type = apply_shape(session, project, new_type)
    session.add(project)
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise IntakeError("This project already has a content source.") from exc
    session.refresh(row)
    session.refresh(project)
    audit_shape(project.id, project.customer_id, old_type, new_type, kind=conn.kind, actor=actor)
    with contextlib.suppress(Exception):
        from app.services import audit

        audit.log(
            actor=actor or "system",
            action="project.made_connected",
            target=f"project:{project.id}",
            detail=f"connection:{conn.id} (existing, no lane, no schedule)",
            customer_id=project.customer_id,
        )
    return row


# ---------------------------------------------------------------------------------------
# return_to
# ---------------------------------------------------------------------------------------
# re.ASCII: `\d` must not match Unicode digits; `\Z`, not `$`, so no trailing newline passes.
_RETURN_RE = re.compile(
    r"^/projects/\d{1,12}(?:/[a-z0-9-]{1,40})?(?:\?[A-Za-z0-9_=&%.\-]{0,200})?(?:#[A-Za-z0-9_-]{1,60})?\Z",
    re.ASCII,
)


def safe_return_to(value) -> str:
    """The `return_to` a form or query may carry, or "" when it is not an in-site project path.
    An allow-list, not a blacklist: `/projects/<id>` (optionally one sub-path, a query and a
    fragment), so no scheme, host, protocol-relative `//`, backslash or control character can
    pass."""
    s = value or ""
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if not s or len(s) > 300 or not _RETURN_RE.match(s):
        return ""
    return s


def with_params(path: str, **params) -> str:
    """Append query params to a validated return_to path (keeps its fragment last)."""
    from urllib.parse import urlencode

    frag = ""
    if "#" in path:
        path, frag = path.split("#", 1)
        frag = "#" + frag
    sep = "&" if "?" in path else "?"
    return path + sep + urlencode({k: v for k, v in params.items() if v is not None}) + frag


def setup_url(project_id: int) -> str:
    """The page where a person connects a source (MCP replies and the CLI point here)."""
    return f"/projects/{project_id}#source"


# ---------------------------------------------------------------------------------------
# read models
# ---------------------------------------------------------------------------------------
def _feed_counts(session: Session) -> tuple[dict[int, int], set[tuple[int, int]]]:
    """(connection id -> number of automations bound to it, the (connection, project) pairs
    those cover). The connection's own legacy project counts too when no automation covers it."""
    out: dict[int, int] = {}
    covered: set[tuple[int, int]] = set()
    for a in session.exec(select(ProjectAutomation)).all():
        out[a.connection_id] = out.get(a.connection_id, 0) + 1
        covered.add((a.connection_id, a.project_id))
    return out, covered


def source_choices(session: Session, project: Project, user=None) -> list[dict]:
    """Step 1: the connections this project may pick, grouped by kind by the caller. Same
    visibility as the bind itself (`connection_visible_to_project`: org, and global or this
    customer's; a lane is never listed) and the same project rule the connection pages apply
    (`resource_visibility.visible_connections`), so a connection the person could not bind is
    never offered, and another customer's connection is not listed at all. Each row carries
    "already feeds N projects"."""
    from app.services import project_automation, resource_visibility

    rows = [
        c
        for c in conn_store.list_connections(customer_id=project.customer_id)
        if project_automation.connection_visible_to_project(c, project)
    ]
    try:
        rows = resource_visibility.visible_connections(session, user, rows)
    except Exception:
        rows = []
    counts, covered = _feed_counts(session)
    out = []
    for c in rows:
        n = counts.get(c.id, 0)
        if c.project_id is not None and (c.id, c.project_id) not in covered:
            n += 1
        sp = registry.spec(c.kind)
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "kind": c.kind,
                "kind_label": sp.name if sp else c.kind,
                "status": c.status,
                "feeds": n,
            }
        )
    out.sort(key=lambda r: (r["kind_label"].lower(), r["name"].lower()))
    return out


def new_system_kinds() -> list[dict]:
    """The systems "Connect a new system" can start (enabled, not archived), by name."""
    out = []
    for sp in registry.active_specs():
        if registry.kind_enabled(sp.kind):
            out.append({"kind": sp.kind, "name": sp.name})
    out.sort(key=lambda r: r["name"].lower())
    return out


def source_labels(session: Session, project_ids) -> dict[int, dict]:
    """For the Projects list: project id -> {intake, kind, label, bound} for CONNECTED projects
    only. `label` is the connector kind name of the bound source, or "Not connected yet"."""
    ids_ = [i for i in (project_ids or []) if i is not None]
    if not ids_:
        return {}
    projects = session.exec(
        select(Project.id).where(Project.id.in_(ids_), Project.intake == ProjectIntake.CONNECTED)
    ).all()
    if not projects:
        return {}
    from app.models import Connection

    autos = {
        a.project_id: a
        for a in session.exec(
            select(ProjectAutomation).where(ProjectAutomation.project_id.in_(list(projects)))
        ).all()
    }
    conn_kind: dict[int, str] = {}
    for cid in {a.connection_id for a in autos.values()}:
        c = session.get(Connection, cid)
        if c is not None:
            conn_kind[cid] = c.kind
    out = {}
    for pid in projects:
        a = autos.get(pid)
        if a is None:
            out[pid] = {
                "intake": "connected",
                "kind": "",
                "label": "Not connected yet",
                "bound": False,
            }
        else:
            kind = conn_kind.get(a.connection_id, "")
            sp = registry.spec(kind) if kind else None
            out[pid] = {
                "intake": "connected",
                "kind": kind,
                "label": (sp.name if sp else kind) or "Connected",
                "bound": True,
            }
    return out


def preview(session: Session, project: Project, row: ProjectAutomation | None) -> dict:
    """Step 6, "Preview": what a first run would meet, with NO egress, NO write and NO spend.

    There is no connector dry-run path (reading the external system is the run itself), so this
    is the readiness check plus the counts and limits already known: whether the run would start,
    the languages it would cover, what the project already holds, the mode, the per-run cap and
    what is left of the monthly budget. The first real run reads the system."""
    from app.models import Connection
    from app.services import project_automation, project_budgets

    out = {
        "ready": False,
        "problems": [],
        "languages": [],
        "documents": 0,
        "mode": "",
        "run_cap_usd": None,
        "budget_remaining_usd": None,
        "note": (
            "Preview reads nothing from the connected system and writes nothing. It checks "
            "that a run could start and shows what is already known; the first run is what "
            "reads the system."
        ),
    }
    if row is None:
        out["problems"].append("No source is connected yet.")
        return out
    out["languages"] = sorted(
        t.locale
        for t in session.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == project.id, TargetLocale.stopped_at.is_(None)
            )
        )
    )
    out["documents"] = session.exec(
        select(func.count()).select_from(Document).where(Document.project_id == project.id)
    ).one()
    out["mode"] = row.mode
    out["run_cap_usd"] = row.run_cap_usd
    if not project_automation.module_enabled():
        out["problems"].append("Project automation is switched off on this install.")
    if getattr(project, "archived_at", None) is not None:
        out["problems"].append("The project is archived.")
    if getattr(project, "ai_opt_out", False):
        out["problems"].append(
            "AI is switched off for this project, so nothing would be translated."
        )
    conn = session.get(Connection, row.run_connection_id)
    if conn is None:
        out["problems"].append("The connection no longer exists.")
    else:
        if not registry.kind_enabled(conn.kind):
            out["problems"].append(f"{conn.kind} connections are switched off on this install.")
        if conn.status == "reauth":
            out["problems"].append("The connection needs to be reconnected.")
    if not out["languages"]:
        out["problems"].append("The project has no target languages yet (step 3).")
    allowed, remaining, msg = project_budgets.project_check(project.id)
    if not allowed:
        out["problems"].append(msg or project_budgets.EXHAUSTED_MESSAGE)
    elif remaining >= 0:
        out["budget_remaining_usd"] = remaining
    out["ready"] = not out["problems"]
    return out


def card_state(
    session: Session,
    project: Project,
    row,
    auto: dict,
    *,
    user=None,
    can_manage: bool,
    want_preview: bool = False,
) -> dict:
    """Everything the project page's Source / setup card needs, as one dict."""
    langs = session.exec(
        select(func.count())
        .select_from(TargetLocale)
        .where(TargetLocale.project_id == project.id, TargetLocale.stopped_at.is_(None))
    ).one()
    bound = bool(auto.get("bound"))
    steps = {
        1: bound,
        2: bound,
        3: bool(langs),
        4: bool(
            bound and (auto.get("enabled") or auto.get("times") or auto.get("interval_minutes"))
        ),
        5: False,  # filled in by the template from notify_view (a route exists)
        6: bool(bound and auto.get("last_status")),
    }
    return {
        "bound": bound,
        "steps": steps,
        "languages": langs,
        "choices": source_choices(session, project, user) if (can_manage and not bound) else [],
        "new_kinds": new_system_kinds() if (can_manage and not bound) else [],
        "preview": preview(session, project, row) if (bound and want_preview) else None,
    }


__all__ = [
    "KEYS_KINDS",
    "UPLOAD_REFUSED",
    "IntakeError",
    "InvalidIntakeError",
    "UploadRefusedError",
    "adopt_new_project",
    "apply_shape",
    "audit_shape",
    "card_state",
    "check_create",
    "content_blockers",
    "convert_candidates",
    "convert_existing",
    "convert_offer",
    "is_connected",
    "new_system_kinds",
    "normalize_intake",
    "preview",
    "refuse_upload",
    "safe_return_to",
    "set_intake",
    "setup_url",
    "shape_for_bind",
    "source_choices",
    "source_labels",
    "with_params",
]
