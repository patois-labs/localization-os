"""`formality` command group: MT formality per target language (#617 item 7, ADR 0062).

  formality registers [--target LOCALE]
  formality show  (--customer ID | --project ID | --document ID)
  formality set   REGISTER --target LOCALE (--customer ID | --project ID | --document ID)
  formality clear --target LOCALE (--customer ID | --project ID | --document ID)

Resolution order for one target language: document override, then project setting, then
customer default, then none (no instruction). REGISTER is formal | informal | auto or a
language-specific register (`formality registers --target de` lists them); an unknown register
for that language is a usage error (exit 2), the same validation as the web form, /api and MCP.

REMOTE (--remote URL --token lsu_): the thin client of /api/formality, scoped server-side by the
token. IN-PROCESS: no per-user RBAC (the operator surface); with multi-org on it needs --org or
--operator, like every in-process write.

Documented in: docs/architecture/style-guides.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    get_scoped_or_404,
    op_org_id,
    remote_client,
    remote_mode,
    require_org_scope,
)


def register(subparsers) -> None:
    fp = subparsers.add_parser(
        "formality",
        help="MT formality (Sie/du, vous/tu, ...) per target language: "
        "customer default, project setting, document override.",
    )
    sub = fp.add_subparsers(dest="formality_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("registers", help="List the registers (the catalog, or one language's).")
    p.add_argument(
        "--target", default="", help="target locale (e.g. de, ja); default: all languages."
    )
    p.set_defaults(func=_registers)

    p = sub.add_parser(
        "show", help="Show a scope's entries and the register in force per language."
    )
    _scope_args(p)
    p.set_defaults(func=_show)

    p = sub.add_parser("set", help="Set the register for one target language at one level.")
    p.add_argument(
        "register_value",
        metavar="REGISTER",
        help="formal | informal | auto, or a language-specific register "
        "(see `formality registers`).",
    )
    p.add_argument("--target", required=True, help="target locale (e.g. de, de-AT, ja).")
    _scope_args(p)
    p.set_defaults(func=_set)

    p = sub.add_parser("clear", help="Clear one entry so the next level applies.")
    p.add_argument("--target", required=True, help="target locale (e.g. de, de-AT, ja).")
    _scope_args(p)
    p.set_defaults(func=_clear)


def _scope_args(p) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--customer", type=bounded_id, default=None, help="customer id (the default).")
    g.add_argument("--project", type=bounded_id, default=None, help="project id.")
    g.add_argument("--document", type=bounded_id, default=None, help="document id (override).")


def _scope_of(args) -> tuple[str, int]:
    if getattr(args, "customer", None) is not None:
        return "customer", args.customer
    if getattr(args, "project", None) is not None:
        return "project", args.project
    return "document", args.document


def _load(session, args, scope, sid):
    """(obj, project, customer) in the operator's --org scope, or the clean 404-as-usage."""
    from app.models import Customer, Document, Project

    org = op_org_id(args)
    if scope == "customer":
        cust = get_scoped_or_404(session.get(Customer, sid), sid, "customer", org)
        return cust, None, cust
    if scope == "project":
        proj = get_scoped_or_404(session.get(Project, sid), sid, "project", org)
        cust = session.get(Customer, proj.customer_id) if proj.customer_id is not None else None
        return proj, proj, cust
    doc = session.get(Document, sid)
    proj = session.get(Project, doc.project_id) if doc is not None else None
    if doc is None or proj is None:
        raise CliError(f"no such document: {sid}", EXIT_USAGE)
    get_scoped_or_404(proj, sid, "document", org)
    cust = session.get(Customer, proj.customer_id) if proj.customer_id is not None else None
    return doc, proj, cust


def _print_rows(emit, data) -> None:
    rows = []
    for r in data.get("rows", []):
        resolved = r.get("resolved") or {}
        rows.append(
            (
                r["locale"],
                r.get("current") or "-",
                resolved.get("label") or "none",
                resolved.get("source") or "-",
            )
        )
    emit.table(rows, headers=("language", "set here", "in force", "from"))


def _registers(args, emit) -> int:
    target = (args.target or "").strip()
    if remote_mode(args):
        params = {"target_locale": target} if target else None
        body = remote_client(args).get("/api/formality/registers", params=params)
        payload = {k: v for k, v in body.items() if k != "ok"}
    else:
        from app.kernel import formality, languages_catalog

        if target:
            if not languages_catalog.is_valid(target):
                raise CliError(
                    f"unknown locale code: {target} (not in the language catalog).", EXIT_USAGE
                )
            payload = {
                "target_locale": languages_catalog.canonical(target),
                "registers": [
                    {"value": r["value"], "label": r["label"], "kind": r["kind"]}
                    for r in formality.registers_for(target)
                ],
            }
        else:
            payload = {"generic": list(formality.GENERIC), "languages": formality.catalog()}
    if emit.as_json:
        emit.json({"ok": True, **payload})
        return 0
    if "registers" in payload:
        emit.say(f"Registers for {payload['target_locale']}:")
        emit.table(
            [(r["value"], r["label"], r["kind"]) for r in payload["registers"]],
            headers=("value", "label", "kind"),
        )
    else:
        emit.say("Valid for every language: " + ", ".join(payload["generic"]) + ".")
        emit.table(
            [
                (
                    lang["language"],
                    ", ".join(r["value"] for r in lang["registers"]),
                    f"formal={lang['formal']} informal={lang['informal']}",
                )
                for lang in payload["languages"]
            ],
            headers=("language", "registers", "generic maps to"),
        )
    return 0


def _show(args, emit) -> int:
    scope, sid = _scope_of(args)
    if remote_mode(args):
        body = remote_client(args).get("/api/formality", params={"scope": scope, "id": sid})
        data = {k: v for k, v in body.items() if k != "ok"}
    else:
        require_org_scope(args, emit)
        from app.kernel import formality

        with _session() as session:
            obj, proj, cust = _load(session, args, scope, sid)
            data = formality.view(session, scope, obj, project=proj, customer=cust)
    if emit.as_json:
        emit.json({"ok": True, **data})
        return 0
    emit.say(f"Formality for {scope} #{sid}:")
    _print_rows(emit, data)
    return 0


def _write(args, emit, value) -> int:
    scope, sid = _scope_of(args)
    actor = getattr(args, "actor", None) or "cli"
    if remote_mode(args):
        body = remote_client(args).post(
            "/api/formality",
            json={"scope": scope, "id": sid, "target_locale": args.target, "register": value},
        )
        res = {k: v for k, v in body.items() if k != "ok"}
    else:
        require_org_scope(args, emit)
        from app.kernel import formality

        with _session() as session:
            obj, proj, _cust = _load(session, args, scope, sid)
            try:
                res = formality.write(
                    session, scope, obj, args.target, value, project=proj, actor=actor
                )
            except formality.FormalityError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
    if res.get("cleared"):
        msg = f"Cleared the formality for {res['target_locale']} on {scope} #{sid}."
    else:
        msg = f"Set formality {res['register']} for {res['target_locale']} on {scope} #{sid}."
    emit.result(msg, {"ok": True, **res})
    return 0


def _set(args, emit) -> int:
    return _write(args, emit, args.register_value)


def _clear(args, emit) -> int:
    return _write(args, emit, None)
