"""`layout` command group: a person's saved page layouts (SPEC-project-layout-editor
section 9, train L1). Web + /api + CLI parity over app/services/page_layouts.py:

  layout list [--user E]                      every layout page and whether it is saved
  layout show <page> [--user E]               the rows of one page (saved or default)
  layout set <page> --rows JSON [--user E]    save rows, e.g. '[["overview"],["tm","glossary"]]'
  layout reset <page> [--user E]              delete the saved layout (back to the default)

REMOTE (--remote URL --token lsu_): the token IS the account, so these act on the
caller's OWN layouts through /api/account/layouts; a non-empty --user is refused
(exit 2), exactly like the `account` group. LOCAL (in-process): --user (email or id)
names the account and is required while sign-in is on; with sign-in OFF there is one
shared layout per page for the install (the "local" owner the web page also uses), so
--user is refused there instead of silently writing a layout no page reads.

Validation is the service's: an unknown page or card, a row of more than 3 cards, a
duplicate or an oversized layout is refused with a plain sentence (exit 2 locally, the
server's 400/404 remotely) and nothing is stored.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import json
from urllib.parse import quote

from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _resolve_user,
    _session,
    local_op,
    remote_client,
    remote_mode,
)

_USER_HELP = (
    "target account (email or id); in-process only, required while sign-in is on - "
    "refused over --remote (the token identifies the account)."
)


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "layout", help="Your saved page layouts (card order and side-by-side rows)."
    )
    sub = grp.add_subparsers(dest="layout_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("list", help="List the layout pages and whether each is saved.")
    p.add_argument("--user", default="", help=_USER_HELP)
    p.set_defaults(func=_layout_list)

    p = sub.add_parser("show", help="Show one page's layout rows (saved or default).")
    p.add_argument("page", help="page key, e.g. project")
    p.add_argument("--user", default="", help=_USER_HELP)
    p.set_defaults(func=_layout_show)

    p = sub.add_parser("set", help="Save a page layout from a JSON list of rows.")
    p.add_argument("page", help="page key, e.g. project")
    p.add_argument(
        "--rows",
        required=True,
        help='JSON list of rows, each 1 to 3 card keys, e.g. \'[["overview"],["tm","glossary"]]\'',
    )
    p.add_argument("--user", default="", help=_USER_HELP)
    p.set_defaults(func=_layout_set)

    p = sub.add_parser("reset", help="Reset a page layout to the default.")
    p.add_argument("page", help="page key, e.g. project")
    p.add_argument("--user", default="", help=_USER_HELP)
    p.set_defaults(func=_layout_reset)


def _refuse_remote_user(args) -> None:
    if (getattr(args, "user", "") or "").strip():
        raise CliError(
            "--user is in-process only; the remote token identifies the account.", EXIT_USAGE
        )


def _local_owner(args):
    """(owner, user_id, label) for an in-process call. Sign-in on: --user is required and
    names the account. Sign-in off: the install's single shared owner, --user refused."""
    from app.kernel import page_layouts, security

    ref = (getattr(args, "user", "") or "").strip()
    if not security.auth_required():
        if ref:
            raise CliError(
                "sign-in is off on this install, so there is one shared layout per page; "
                "drop --user.",
                EXIT_USAGE,
            )
        owner, uid = page_layouts.owner_for_user(None)
        return owner, uid, "this install"
    if not ref:
        raise CliError(
            "in-process 'layout' needs --user <email-or-id> while sign-in is on. "
            "Over --remote the token identifies the account.",
            EXIT_USAGE,
        )
    with _session() as s:
        u = _resolve_user(s, ref)
        owner, uid = page_layouts.owner_for_user(u)
        return owner, uid, u.email


def _api_path(page: str) -> str:
    return "/api/account/layouts/" + quote(str(page), safe="")


def _rows_text(rows) -> str:
    return " | ".join(" + ".join(r) for r in rows)


def _say_layout(emit, view: dict) -> None:
    state = "saved" if view.get("saved") else "default"
    emit.say(f"{view.get('title', view.get('page'))} ({view.get('page')}): {state}")
    cards = view.get("cards") or {}
    for i, row in enumerate(view.get("rows") or [], start=1):
        names = ", ".join(f"{cards.get(c, c)} [{c}]" for c in row)
        emit.say(f"  row {i}: {names}")


def _parse_rows(raw: str):
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise CliError("--rows is not valid JSON.", EXIT_USAGE) from exc


# --------------------------------------------------------------------------- #
# Leaves
# --------------------------------------------------------------------------- #
def _layout_list(args, emit) -> int:
    if remote_mode(args):
        _refuse_remote_user(args)
        body = remote_client(args).get("/api/account/layouts")
        layouts = body.get("layouts") or []
    else:
        local_op(args, emit)
        from app.kernel import page_layouts

        owner, _uid, _label = _local_owner(args)
        layouts = page_layouts.list_layouts(owner)
    if emit.as_json:
        emit.json({"ok": True, "layouts": layouts})
    else:
        for view in layouts:
            state = "saved" if view.get("saved") else "default"
            emit.say(f"{view.get('page')}: {state}  {_rows_text(view.get('rows') or [])}")
    return 0


def _layout_show(args, emit) -> int:
    if remote_mode(args):
        _refuse_remote_user(args)
        view = remote_client(args).get(_api_path(args.page))
        view = {k: v for k, v in view.items() if k != "ok"}
    else:
        local_op(args, emit)
        from app.kernel import page_layouts

        owner, _uid, _label = _local_owner(args)
        try:
            view = page_layouts.get_layout(owner, args.page)
        except page_layouts.UnknownPage as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    if emit.as_json:
        emit.json({"ok": True, **view})
    else:
        _say_layout(emit, view)
    return 0


def _layout_set(args, emit) -> int:
    rows = _parse_rows(args.rows)
    if remote_mode(args):
        _refuse_remote_user(args)
        view = remote_client(args).put(_api_path(args.page), json={"rows": rows})
        view = {k: v for k, v in view.items() if k != "ok"}
    else:
        local_op(args, emit)
        from app.kernel import audit, page_layouts

        owner, uid, label = _local_owner(args)
        try:
            view = page_layouts.save_layout(owner, uid, args.page, {"rows": rows})
        except (page_layouts.UnknownPage, page_layouts.LayoutError) as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(actor=_actor(args), action="account.layout_save", target=args.page, detail=label)
    emit.result(
        f"Saved the {view.get('page')} layout: {_rows_text(view.get('rows') or [])}",
        {"ok": True, **view},
    )
    return 0


def _layout_reset(args, emit) -> int:
    if remote_mode(args):
        _refuse_remote_user(args)
        view = remote_client(args).delete(_api_path(args.page))
        view = {k: v for k, v in view.items() if k != "ok"}
    else:
        local_op(args, emit)
        from app.kernel import audit, page_layouts

        owner, _uid, label = _local_owner(args)
        try:
            existed = page_layouts.reset_layout(owner, args.page)
            view = {"reset": existed, **page_layouts.get_layout(owner, args.page)}
        except page_layouts.UnknownPage as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(actor=_actor(args), action="account.layout_reset", target=args.page, detail=label)
    note = "" if view.get("reset") else " (nothing was saved)"
    emit.result(f"Reset the {view.get('page')} layout to the default{note}.", {"ok": True, **view})
    return 0
