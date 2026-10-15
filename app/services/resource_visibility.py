"""Who may see a workflow or a connection: the rules the list pages and global search share.

Moved out of the web layer (#621) so the Workflows list, the Connectors list and global search
cannot drift on what a person may open. Nothing here changed behavior; the web helpers
(`workflows_common._visible_workflows`, `connectors_common._project_visible_connections`) are
now one-line wrappers.

Two shapes of the same rules:

  * Python filters over rows already loaded (`visible_workflows`, `visible_connections`), used
    by the list pages;
  * SQL conditions (`workflow_conditions`, `connection_conditions`) that global search applies
    BEFORE it counts, so a hit or a count never exists for a row the caller cannot open.

The rules mirror the object pages exactly (the route-level `_wf_or_404` and `_conn_or_404`):

  workflow    its org ring (multi-org), the customer ring (a customer-bound caller sees global
              workflows and its own customer's), and the project ring when the workflow is
              bound to a project;
  connection  the same three rings, plus the connector KIND must be enabled on the install
              (a disabled kind's connections 404 on every page) and a bound project must still
              exist.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from sqlmodel import select

from app.models import Connection, Project, Workflow
from app.services import access


def visible_workflows(session, user, workflows):
    """Keep workflow lists inside the same org/customer/project rings as detail."""
    cid = access.current_customer_id(user)
    allowed = access.visible_project_ids(session, user)
    allowed = set(allowed) if allowed is not None else None
    return [
        w
        for w in workflows
        if access.same_org(user, w.org_id)
        and not access.customer_ring_blocks(cid, w.customer_id)
        and (w.project_id is None or allowed is None or w.project_id in allowed)
    ]


def visible_connections(session, user, rows):
    """#474: drop connections bound to a project the user cannot see (the project rule the
    per-connection page already applies, #370). Unrestricted users (staff, auth off) keep
    every row."""
    visible = access.visible_project_ids(session, user)
    if visible is None:
        return rows
    visible = set(visible)
    return [c for c in rows if c.project_id is None or c.project_id in visible]


def _org_condition(column, org_id):
    """SQL for `access.same_org`: None means "no org filter" (gate off or the authorized
    operator door); a concrete org keeps only that org's rows (an un-stamped row is
    quarantined under an active gate, exactly like same_org)."""
    value = access.org_scope_value(org_id)
    return None if value is None else column == value


def _project_condition(column, project_ids, *, must_exist: bool = False):
    """SQL for the project ring. A row bound to no project is always inside it. `project_ids`
    None is the unrestricted caller: any bound project passes (`must_exist` additionally
    requires the project row to exist, like the connection page)."""
    unbound = column.is_(None)
    if project_ids is None:
        return (unbound | column.in_(select(Project.id))) if must_exist else None
    return unbound | column.in_(list(project_ids))


def workflow_conditions(*, org_id, customer_id, project_ids) -> list | None:
    """WHERE conditions for the workflows a caller may open, or None when it may open none
    (the fail-closed org case). Archived workflows are left out, like the default list."""
    if access.org_scope_blocked(org_id):
        return None
    conds = [Workflow.archived_at.is_(None)]
    for cond in (
        _org_condition(Workflow.org_id, org_id),
        # a GLOBAL workflow (no customer) is open to every bound caller; a customer's own is not
        None
        if customer_id is None
        else (Workflow.customer_id.is_(None) | (Workflow.customer_id == customer_id)),
        _project_condition(Workflow.project_id, project_ids),
    ):
        if cond is not None:
            conds.append(cond)
    return conds


def connection_conditions(*, org_id, customer_id, project_ids, disabled_kinds=()) -> list | None:
    """WHERE conditions for the connections a caller may open, or None when it may open none.
    `disabled_kinds` are the connector kinds an admin switched off on this install."""
    if access.org_scope_blocked(org_id):
        return None
    conds = []
    for cond in (
        _org_condition(Connection.org_id, org_id),
        None
        if customer_id is None
        else (Connection.customer_id.is_(None) | (Connection.customer_id == customer_id)),
        _project_condition(Connection.project_id, project_ids, must_exist=True),
        Connection.kind.not_in(list(disabled_kinds)) if disabled_kinds else None,
    ):
        if cond is not None:
            conds.append(cond)
    return conds
