"""`content-type` command group: content-type CRUD (SPEC-content-types, phase 1).

In-process (local-root, no RBAC — same trust as editing the DB directly) OR --remote
(a thin HTTP client of /api/content-types, org-scoped ENTIRELY by --token server-side).
Business logic lives in app.services.content_types; this module is a thin CLI shell.
ASCII-safe output (no em-dashes/emoji — Windows consoles).

Documented in: docs/architecture/content-and-projects.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    op_org_id,
    op_org_scope,
    remote_mode,
    require_org_scope,
)
from ._common import get_customer as _get_customer


def register(subparsers) -> None:
    ct = subparsers.add_parser(
        "content-type",
        help="Content types (per-customer domain scoping: create, list, rename, delete).",
    )
    sub = ct.add_subparsers(dest="content_type_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create a content type (global, or --customer).")
    p.add_argument("name", help="content type name.")
    p.add_argument("--description", default="", help="optional description.")
    p.add_argument(
        "--customer",
        type=bounded_id,
        default=None,
        help="customer scope id (omit = global / offered to everyone).",
    )
    p.set_defaults(func=_ct_create)

    p = sub.add_parser("list", help="List content types (global + customer-scoped).")
    p.set_defaults(func=_ct_list)

    p = sub.add_parser("rename", help="Rename a content type.")
    p.add_argument("content_type_id", type=bounded_id, help="content type id.")
    p.add_argument("name", help="the new name.")
    p.set_defaults(func=_ct_rename)

    p = sub.add_parser("delete", help="Delete a content type (refused while referenced).")
    p.add_argument("content_type_id", type=bounded_id, help="content type id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_ct_delete)


def _visible(session, args):
    """Fetch a content type in the operator's --org scope, or 404-as-usage (mirrors
    _gmod_get: an --org A operator must not rename/delete org B's type).

    #73 COUPLING: op_org_SCOPE — ct.visible now fails closed on a bare None under an
    active gate, so the --operator door hands it access.UNSCOPED (not a bare None) to
    keep the cross-org read."""
    from app.kernel import content_types as ct

    c = ct.visible(session, args.content_type_id, org_id=op_org_scope(args))
    if c is None:
        raise CliError(f"no such content type: {args.content_type_id}", EXIT_USAGE)
    return c


def _ct_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.content_type_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import content_types as ct

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        customer_id = getattr(args, "customer", None)
        if customer_id is not None:
            # A --customer must belong to the operator's --org scope (a foreign
            # customer is rejected, not silently stamped) — mirrors _gmod_create.
            _get_customer(session, customer_id, op_org_id(args))
        try:
            c = ct.create(
                session,
                args.name,
                description=args.description,
                org_id=op_org_id(args),
                customer_id=customer_id,
                created_by=actor,
            )
        except ct.ContentTypeError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="content_type.create",
            target=f"content_type {c.id} '{c.name}'",
            customer_id=c.customer_id,
        )
        emit.result(
            f"Created content type #{c.id}: {c.name}"
            + (f" (customer {c.customer_id})" if c.customer_id else " (global)"),
            {"ok": True, **ct.to_dict(session, c)},
        )
    return 0


def _ct_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.content_type_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content_types as ct

    with _session() as session:
        # #73 COUPLING: op_org_SCOPE so the --operator door (access.UNSCOPED) still lists
        # every org; list_types now fails closed on a bare None under an active gate.
        types = ct.list_types(session, org_id=op_org_scope(args))
        rows, payload = [], []
        for c in types:
            d = ct.to_dict(session, c)
            rows.append(
                (
                    c.id,
                    c.name,
                    c.customer_id if c.customer_id else "global",
                    d["references"]["total"],
                )
            )
            payload.append(d)
        if emit.as_json:
            emit.json({"ok": True, "content_types": payload})
        else:
            emit.table(rows, headers=("id", "name", "scope", "refs"))
    return 0


def _ct_rename(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.content_type_rename(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import content_types as ct

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        old_name = _visible(session, args).name  # captured BEFORE rename mutates it
        try:
            c = ct.rename(session, args.content_type_id, args.name)
        except ct.ContentTypeError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="content_type.rename",
            target=f"content_type {c.id}",
            customer_id=c.customer_id,
            detail=f"{old_name!r} -> {c.name!r}",
        )
        emit.result(
            f"Renamed content type #{c.id} to {c.name}.", {"ok": True, **ct.to_dict(session, c)}
        )
    return 0


def _ct_delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.content_type_delete(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import content_types as ct

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        c = _visible(session, args)
        if not confirm(emit, f"Delete content type #{c.id} ({c.name})?", args.yes):
            raise CliError("aborted.", 1)
        try:
            ct.delete(session, args.content_type_id)  # FAILS CLOSED while referenced
        except ct.ContentTypeError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="content_type.delete",
            target=f"content_type {args.content_type_id}",
            customer_id=c.customer_id,
        )
        emit.result(
            f"Deleted content type #{args.content_type_id}.",
            {"ok": True, "content_type_id": args.content_type_id, "deleted": True},
        )
    return 0
