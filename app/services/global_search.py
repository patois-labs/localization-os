"""Global search: one bounded, access-scoped query over projects, documents, TM, glossaries,
workflows and connections.

The single seam the web results page (`/search`), the live suggestions (`/search/suggest`),
`GET /api/search`, the `search` CLI verb and the MCP `search_workspace` tool all call, so the five
surfaces cannot drift on what is searched, who may see it, or how big an answer can get.

WHAT IS SEARCHED (the placeholder and the empty states say exactly this):
  * projects    -- project name and its customer's name (archived projects are left out, like
                   the Projects list);
  * documents   -- file name (not the text inside the file); a key project's internal
                   `keys` documents are never listed, like every document route;
  * tm          -- translation-memory source and target text;
  * glossary    -- glossary term source and target text of the glossaries the caller may open;
  * workflows   -- workflow name (archived workflows are left out, like the default list);
  * connections -- connection name and connector kind (a disabled kind's connections are left
                   out). Credentials and settings are never searched or shown.

ACCESS: a `Scope` is built ONCE per call from the same authorities the list pages use:
`access.visible_project_ids` (org ring, project membership, customer binding) for projects,
documents and TM (TM rows are scoped through their project, exactly like `/tm`; rows with no
project belong to no customer and are never searched), and the glossary module's org +
customer rules for glossaries (a customer-bound caller sees only its own customer's
glossaries; glossaries with no customer are staff-only, like the Glossaries page). Workflows
and connections follow the rules of their own pages (org ring, customer ring, project ring;
a connection also needs an enabled connector kind), shared with the list pages through
`app.services.resource_visibility`. Hidden rows are filtered in SQL BEFORE counting, so a
count never leaks a hidden item.

BOUNDS: the query is at least MIN_QUERY and at most MAX_QUERY characters; every group returns
at most MAX_LIMIT hits per call and counts to COUNT_CAP (shown as "1000+"); the TM scan is
limited to the newest TM_SCAN_CAP rows of the caller's scope (reported as `truncated`) so a
huge TM cannot hold the server on a substring scan; paging stops at MAX_OFFSET.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from dataclasses import dataclass

from sqlalchemy import case, func
from sqlmodel import Session, select

from app import config
from app.models import (
    Connection,
    Customer,
    Document,
    Glossary,
    GlossaryEntry,
    Project,
    ProjectStatus,
    SegmentTM,
    Workflow,
)
from app.services import access, resource_visibility, security
from app.services import customers as customer_service

MIN_QUERY = 2
MAX_QUERY = 100
DEFAULT_LIMIT = 5
MAX_LIMIT = 25
MAX_OFFSET = 1000
COUNT_CAP = 1000
TM_SCAN_CAP = 100_000
SNIPPET = 140

GROUPS = ("projects", "documents", "tm", "glossary", "workflows", "connections")

LABELS = {
    "projects": "Projects",
    "documents": "Documents",
    "tm": "Translation memory",
    "glossary": "Glossary terms",
    "workflows": "Workflows",
    "connections": "Connections",
}

#: One honest sentence about what is searched, shared by every surface.
SEARCHED = (
    "Project and customer names, document file names, translation memory text, "
    "glossary terms, workflow names and connection names. Text inside documents is not searched."
)


class SearchQueryError(ValueError):
    """The query is unusable (too short, too long, or an unknown group). The message is a
    plain sentence meant for the person; every surface shows it as a 400 / usage error."""


@dataclass(frozen=True)
class Scope:
    """What the caller may see. Built by `scope_for_user` / `scope_for_org`, never by hand
    from request input.

    project_ids        visible project ids, or None for unrestricted (single-tenant / see-all);
    org_id             the org ring for glossaries and customers (None = no org filter);
    customer_id        a customer-bound caller's customer, or None;
    unassigned_glossaries  may the caller see glossaries that belong to no customer."""

    project_ids: list | None
    org_id: object = None
    customer_id: int | None = None
    unassigned_glossaries: bool = False


def scope_for_user(session: Session, user) -> Scope:
    """The scope of a signed-in web / API caller (or the SYSTEM operator with sign-in off)."""
    cid = access.current_customer_id(user)
    manager = (not security.auth_required()) or security.can_manage(user)
    return Scope(
        project_ids=access.visible_project_ids(session, user),
        org_id=access.current_org_id(user),
        customer_id=cid,
        unassigned_glossaries=bool(cid is None and manager),
    )


def scope_for_org(session: Session, org_id) -> Scope:
    """The in-process CLI operator scope: one org's projects (or everything with org_id None,
    i.e. single-tenant or the key-verified `--operator` door). The CLI has no per-user RBAC."""
    if access.org_scope_blocked(org_id):
        return Scope(project_ids=[], org_id=org_id, customer_id=None)
    pids = None
    if org_id is not None and org_id is not access.UNSCOPED:
        pids = list(session.exec(select(Project.id).where(Project.org_id == org_id)).all())
    # None here means the operator is authorized to sweep every org (or single-tenant): say so
    # explicitly, because a bare None under an active multi-org gate reads as "broken principal".
    return Scope(
        project_ids=pids,
        org_id=access.UNSCOPED if org_id is None else org_id,
        customer_id=None,
        unassigned_glossaries=True,
    )


def clean_query(raw) -> str:
    """Trim and collapse whitespace, then enforce the length bounds (raises SearchQueryError)."""
    q = " ".join(str(raw or "").split())
    if len(q) < MIN_QUERY:
        raise SearchQueryError(f"Type at least {MIN_QUERY} characters to search.")
    if len(q) > MAX_QUERY:
        raise SearchQueryError(f"Search text is limited to {MAX_QUERY} characters.")
    return q


def _ci(col):
    """The column as a case-insensitive LIKE operand. SQLite's LIKE already ignores ASCII case
    and so does its lower() (ASCII only), so `lower(col) LIKE pat` and `col LIKE pat` match the
    same rows there; dropping the per-row lower() call made the full-table substring scan
    2.6 times faster (100k rows: 122 ms -> 46 ms, #608). Other backends have a case-sensitive
    LIKE and keep lower()."""
    return col if config.IS_SQLITE else func.lower(col)


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clip(text, n: int = SNIPPET) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "..."


def _capped_count(session: Session, ids_stmt) -> tuple[int, bool]:
    """(count, capped): rows matching `ids_stmt` (a one-column select), counted to COUNT_CAP."""
    sub = ids_stmt.limit(COUNT_CAP + 1).subquery()
    n = session.exec(select(func.count()).select_from(sub)).one()
    return (min(n, COUNT_CAP), n > COUNT_CAP)


def _page_ids(session: Session, base, rank_expr, id_col, limit: int, offset: int):
    """(count, capped, page_ids) from ONE scan of the matching rows (#608).

    Each group used to scan twice: once to count (to COUNT_CAP matches) and again to find the
    page (`id IN (the same filter, newest COUNT_CAP + 1)`). On a large TM the scan IS the cost
    and every concurrent caller paid it twice. The newest COUNT_CAP + 1 matching ids are now
    read once together with their rank (starts-with hits first); the count, the ordering
    (rank, then newest id first) and the page all come from that list, so the answer is the
    same as the two-scan form. Only the page's rows are loaded afterwards, by primary key."""
    found = session.execute(
        base.add_columns(rank_expr).order_by(id_col.desc()).limit(COUNT_CAP + 1)
    ).all()
    n = len(found)
    found = [(r[0], r[1]) for r in found]
    found.sort(key=lambda r: (r[1], -r[0]))
    return min(n, COUNT_CAP), n > COUNT_CAP, [r[0] for r in found[offset : offset + limit]]


def _in_order(rows, ids, pos: int = 0):
    """`rows` (tuples whose item `pos` is the id) in the order of `ids`."""
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r[pos]])


def _rank(col, prefix_pat: str):
    """Sort hits that START with the query ahead of hits that merely contain it."""
    return case((_ci(col).like(prefix_pat, escape="\\"), 0), else_=1)


def _empty(key: str) -> dict:
    return {"key": key, "label": LABELS[key], "count": 0, "capped": False, "hits": []}


def _projects(session, scope, pat, prefix, limit, offset) -> dict:
    out = _empty("projects")
    if scope.project_ids is not None and not scope.project_ids:
        return out
    cust_ids = select(Customer.id).where(_ci(Customer.name).like(pat, escape="\\"))
    cond = _ci(Project.name).like(pat, escape="\\") | Project.customer_id.in_(cust_ids)
    base = select(Project.id).where(cond, Project.archived_at.is_(None))
    if scope.project_ids is not None:
        base = base.where(Project.id.in_(scope.project_ids))
    out["count"], out["capped"], page = _page_ids(
        session, base, _rank(Project.name, prefix), Project.id, limit, offset
    )
    if out["count"] == 0 or offset >= out["count"]:
        return out
    rows = _in_order(
        session.exec(
            select(Project.id, Project.name, Project.status, Customer.name)
            .join(Customer, Customer.id == Project.customer_id, isouter=True)
            .where(Project.id.in_(page))
        ).all(),
        page,
    )
    for pid, name, status, cust in rows:
        label = ProjectStatus.LABELS.get(status, status or "")
        out["hits"].append(
            {
                "kind": "project",
                "id": pid,
                "title": name,
                "subtitle": " - ".join(x for x in (cust, label) if x),
                "href": f"/projects/{pid}",
            }
        )
    return out


def _documents(session, scope, pat, prefix, limit, offset) -> dict:
    out = _empty("documents")
    if scope.project_ids is not None and not scope.project_ids:
        return out
    base = select(Document.id).where(
        _ci(Document.filename).like(pat, escape="\\"), Document.fmt != "keys"
    )
    if scope.project_ids is not None:
        base = base.where(Document.project_id.in_(scope.project_ids))
    out["count"], out["capped"], page = _page_ids(
        session, base, _rank(Document.filename, prefix), Document.id, limit, offset
    )
    if out["count"] == 0 or offset >= out["count"]:
        return out
    rows = _in_order(
        session.exec(
            select(Document.id, Document.filename, Project.name)
            .join(Project, Project.id == Document.project_id)
            .where(Document.id.in_(page))
        ).all(),
        page,
    )
    for did, filename, pname in rows:
        out["hits"].append(
            {
                "kind": "document",
                "id": did,
                "title": filename,
                "subtitle": pname,
                "href": f"/documents/{did}",
            }
        )
    return out


def _tm_scope(scope, *, walk_by_id: bool = False):
    cond = [SegmentTM.project_id.is_not(None)]
    if scope.project_ids is not None:
        # `project_id + 0` keeps the planner off the project_id index for the window
        # query: with the index it sorts EVERY row of the caller's projects to find the
        # newest TM_SCAN_CAP (seconds on a million-row project); without it, it walks the
        # primary key newest-first and stops at the cap.
        col = (SegmentTM.project_id + 0) if walk_by_id else SegmentTM.project_id
        cond.append(col.in_(scope.project_ids))
    return cond


def _tm(session, scope, pat, prefix, limit, offset) -> dict:
    out = _empty("tm")
    out["truncated"] = False
    if scope.project_ids is not None and not scope.project_ids:
        return out
    # Bound the substring scan to the newest TM_SCAN_CAP rows of the caller's own scope.
    # First a cheap size check (an index-only count that stops past the cap). Only when the
    # scope is bigger than the cap, find the first row PAST the window once (a newest-first
    # id walk, no sort); every later statement is then an id range, so no statement scans
    # or sorts beyond the window.
    edge = None
    size = select(SegmentTM.id).where(*_tm_scope(scope)).limit(TM_SCAN_CAP + 1).subquery()
    if session.exec(select(func.count()).select_from(size)).one() > TM_SCAN_CAP:
        edge = session.exec(
            select(SegmentTM.id)
            .where(*_tm_scope(scope, walk_by_id=True))
            .order_by(SegmentTM.id.desc())
            .offset(TM_SCAN_CAP)
            .limit(1)
        ).first()
    out["truncated"] = edge is not None
    match = _ci(SegmentTM.source_text).like(pat, escape="\\") | _ci(SegmentTM.target_text).like(
        pat, escape="\\"
    )
    if edge is None:
        base = select(SegmentTM.id).where(*_tm_scope(scope), match)
    else:
        # An id range over the window only (the primary key, not the project index).
        base = select(SegmentTM.id).where(
            SegmentTM.id > edge, *_tm_scope(scope, walk_by_id=True), match
        )
    out["count"], out["capped"], page = _page_ids(
        session, base, _rank(SegmentTM.source_text, prefix), SegmentTM.id, limit, offset
    )
    if out["count"] == 0 or offset >= out["count"]:
        return out
    rows = _in_order(
        session.exec(
            select(
                SegmentTM.id,
                SegmentTM.source_text,
                SegmentTM.target_text,
                SegmentTM.source_locale,
                SegmentTM.target_locale,
                Project.name,
                Project.customer_id,
            )
            .join(Project, Project.id == SegmentTM.project_id)
            .where(SegmentTM.id.in_(page))
        ).all(),
        page,
    )
    customers = set()
    for tid, src, tgt, sl, tl, pname, cid in rows:
        customers.add(cid)
        out["hits"].append(
            {
                "kind": "tm",
                "id": tid,
                "title": _clip(src),
                "subtitle": _clip(tgt),
                "detail": f"{sl} to {tl} - {pname}",
                "href": f"/tm/{tid}" + (f"?customer={cid}" if cid else ""),
            }
        )
    only = next(iter(customers)) if len(customers) == 1 else None
    if only is not None and (
        session.exec(
            customer_service.active_only(select(Customer.id)).where(Customer.id == only)
        ).first()
        is None
    ):
        only = None  # the TM page browses no archived customer, so "open the list" would dead-end
    out["customer_id"] = only
    return out


def _visible_glossary_ids(scope):
    """Subquery of glossary ids the caller may open: the glossary module's org ring, the
    customer binding, active customers only (the Glossaries page lists no others), and
    customer-less glossaries for staff."""
    if access.org_scope_blocked(scope.org_id):
        return None
    org_id = access.org_scope_value(scope.org_id)
    cust = customer_service.active_only(select(Customer.id))
    if org_id is not None:
        cust = cust.where(Customer.org_id == org_id)
    if scope.customer_id is not None:
        cust = cust.where(Customer.id == scope.customer_id)
    cond = Glossary.customer_id.in_(cust)
    if scope.unassigned_glossaries and scope.customer_id is None:
        cond = cond | Glossary.customer_id.is_(None)
    stmt = select(Glossary.id).where(cond)
    if org_id is not None:
        stmt = stmt.where(Glossary.org_id == org_id)
    return stmt


def _glossary(session, scope, pat, prefix, limit, offset) -> dict:
    out = _empty("glossary")
    visible = _visible_glossary_ids(scope)
    if visible is None:
        return out
    match = _ci(GlossaryEntry.source_term).like(pat, escape="\\") | _ci(
        GlossaryEntry.target_term
    ).like(pat, escape="\\")
    base = select(GlossaryEntry.id).where(GlossaryEntry.glossary_id.in_(visible), match)
    out["count"], out["capped"], page = _page_ids(
        session, base, _rank(GlossaryEntry.source_term, prefix), GlossaryEntry.id, limit, offset
    )
    if out["count"] == 0 or offset >= out["count"]:
        return out
    rows = _in_order(
        session.exec(
            select(
                GlossaryEntry.id,
                GlossaryEntry.source_term,
                GlossaryEntry.target_term,
                GlossaryEntry.source_locale,
                GlossaryEntry.target_locale,
                GlossaryEntry.glossary_id,
                Glossary.name,
            )
            .join(Glossary, Glossary.id == GlossaryEntry.glossary_id)
            .where(GlossaryEntry.id.in_(page))
        ).all(),
        page,
    )
    for eid, src, tgt, sl, tl, gid, gname in rows:
        out["hits"].append(
            {
                "kind": "glossary",
                "id": eid,
                "title": _clip(src),
                "subtitle": _clip(tgt) if tgt else "Do not translate",
                "detail": f"{sl} to {tl} - {gname}",
                "href": f"/glossaries/{gid}",
            }
        )
    return out


def _workflows(session, scope, pat, prefix, limit, offset) -> dict:
    out = _empty("workflows")
    conds = resource_visibility.workflow_conditions(
        org_id=scope.org_id, customer_id=scope.customer_id, project_ids=scope.project_ids
    )
    if conds is None:
        return out
    base = select(Workflow.id).where(*conds, _ci(Workflow.name).like(pat, escape="\\"))
    out["count"], out["capped"], page = _page_ids(
        session, base, _rank(Workflow.name, prefix), Workflow.id, limit, offset
    )
    if out["count"] == 0 or offset >= out["count"]:
        return out
    rows = _in_order(
        session.exec(
            select(
                Workflow.id,
                Workflow.name,
                Workflow.source_locale,
                Workflow.target_locale,
                Customer.name,
            )
            .join(Customer, Customer.id == Workflow.customer_id, isouter=True)
            .where(Workflow.id.in_(page))
        ).all(),
        page,
    )
    for wid, name, sl, tl, cust in rows:
        out["hits"].append(
            {
                "kind": "workflow",
                "id": wid,
                "title": name,
                "subtitle": cust or "All customers",
                "detail": f"{sl} to {tl}" if sl and tl else "",
                "href": f"/workflows/{wid}",
            }
        )
    return out


def _kind_name(kind) -> str:
    """The connector's display name ("Airtable"), or the stored slug for an unknown kind."""
    from app.services import connectors

    cls = connectors.REGISTRY.get(kind)
    return cls.spec.name if cls is not None else str(kind or "")


def _connections(session, scope, pat, prefix, limit, offset) -> dict:
    from app.services import connectors

    out = _empty("connections")
    conds = resource_visibility.connection_conditions(
        org_id=scope.org_id,
        customer_id=scope.customer_id,
        project_ids=scope.project_ids,
        disabled_kinds=sorted(connectors.disabled_kinds()),
    )
    if conds is None:
        return out
    # Name or connector kind (a search for "jira" finds the Jira connections). Only these two
    # plain columns are matched: config and credentials are never read.
    match = _ci(Connection.name).like(pat, escape="\\") | _ci(Connection.kind).like(
        pat, escape="\\"
    )
    base = select(Connection.id).where(*conds, match)
    out["count"], out["capped"], page = _page_ids(
        session, base, _rank(Connection.name, prefix), Connection.id, limit, offset
    )
    if out["count"] == 0 or offset >= out["count"]:
        return out
    rows = _in_order(
        session.exec(
            select(Connection.id, Connection.name, Connection.kind, Connection.status).where(
                Connection.id.in_(page)
            )
        ).all(),
        page,
    )
    for cid, name, kind, status in rows:
        out["hits"].append(
            {
                "kind": "connection",
                "id": cid,
                "title": name,
                "subtitle": _kind_name(kind),
                "detail": str(status or ""),
                "href": f"/connectors/connection/{cid}",
            }
        )
    return out


_RUNNERS = {
    "projects": _projects,
    "documents": _documents,
    "tm": _tm,
    "glossary": _glossary,
    "workflows": _workflows,
    "connections": _connections,
}


def search(
    session: Session,
    scope: Scope,
    q,
    *,
    groups=None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Search every requested group (default: all) under `scope`.

    Returns {"query", "searched", "groups": [{key, label, count, capped, hits[], list_href?}],
    "total", "limit", "offset"}. Raises SearchQueryError for a bad query or group name."""
    text = clean_query(q)
    wanted = list(groups) if groups else list(GROUPS)
    for g in wanted:
        if g not in _RUNNERS:
            raise SearchQueryError(f"Unknown search group {g!r}. Use one of: {', '.join(GROUPS)}.")
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        raise SearchQueryError("limit and offset must be whole numbers.") from None
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, min(offset, MAX_OFFSET))
    esc = _like_escape(text.lower())
    pat, prefix = f"%{esc}%", f"{esc}%"
    result = []
    for g in dict.fromkeys(wanted):
        block = _RUNNERS[g](session, scope, pat, prefix, limit, offset)
        if g == "projects":
            from urllib.parse import quote

            block["list_href"] = f"/projects?status=all&q={quote(text, safe='')}"
        elif g == "tm" and block.get("customer_id"):
            from urllib.parse import quote

            block["list_href"] = f"/tm?customer={block['customer_id']}&q={quote(text, safe='')}"
        block.pop("customer_id", None)
        result.append(block)
    return {
        "query": text,
        "searched": SEARCHED,
        "groups": result,
        "total": sum(b["count"] for b in result),
        "limit": limit,
        "offset": offset,
    }
