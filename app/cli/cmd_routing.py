"""`routing` command group (ffadmin Wave 4): the feature-gated top-level twin of the
web /routing admin surface + the /api/routing/rules endpoints.

The maintainer's Wave-4 decision: close the pre-existing web/CLI parity hole. The routing-rule
CRUD previously lived ONLY under the `admin routing rule ...` subgroup -- and `admin` is
the CORE, un-gateable admin spine (a feature may never claim it), so the `routing` feature
had no CLI group of its own to switch off. This module is that group: a genuine twin of
/routing (rule list / create / toggle / delete) + read-back (show), gated by the
`routing_enabled` kill switch. `admin routing rule ...` stays as the admin-console twin;
this is the FEATURE surface the switch removes.

  routing rule add [--ext E --department D --priority N --project ID --provider P]
                   (--subject/--workflow/--language-set are RESERVED for v2 and REFUSED)
  routing rule list
  routing rule show <rule-id>
  routing rule toggle <rule-id>
  routing rule delete <rule-id> [--yes]

DEPARTMENT BUDGETS ARE NOT HERE. The web /routing page also renders per-department
budgets, but budgets are CORE, not part of the `routing` feature (plan Q4) -- they
keep their own `admin budget ...` group so a routing switch-off never gates spend control.

Full REMOTE parity (--remote --token): each verb hits its /api/routing/rules endpoint.
The dispatch gate + this layer-2 _flag_gate refuse the LOCAL arm when routing is off; a
--remote command is answered by the server, whose own flag state is authoritative (its
/api/routing/rules 404s, mapped to a clean "server: not found").

Documented in: docs/architecture/cost-and-budgets.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _session,
    bounded_id,
    confirm,
    op_org_id,
    remote_mode,
    require_org_scope,
)


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "routing",
        help="Content-routing rules (add/list/show/toggle/delete) -- the "
        "feature twin of the /routing page. Department budgets stay under `admin budget`.",
    )
    sub = grp.add_subparsers(dest="routing_cmd", metavar="<subcommand>")
    sub.required = True  # a bare `routing` shows the SCOPED usage (house convention).

    rule = sub.add_parser("rule", help="Routing rules.")
    rsub = rule.add_subparsers(dest="rule_cmd", metavar="<subcommand>")
    rsub.required = True

    p = rsub.add_parser("add", help="Create a routing rule.")
    p.add_argument("--ext", default="", help="match file extension(s), comma-sep (blank = any).")
    p.add_argument(
        "--subject",
        default="",
        help="RESERVED for v2, REFUSED -- subject matching is not consulted at run time yet.",
    )
    p.add_argument("--department", default="", help="match submitter department (blank = any).")
    p.add_argument("--priority", type=int, default=100, help="lower wins (default 100).")
    p.add_argument("--project", type=bounded_id, default=None, help="target project id.")
    p.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="RESERVED for v2, REFUSED -- workflow targeting is not consulted at run time yet.",
    )
    p.add_argument(
        "--language-set",
        dest="language_set",
        type=bounded_id,
        default=None,
        help="RESERVED for v2, REFUSED -- language-set targeting is not consulted at run time yet.",
    )
    p.add_argument("--provider", default="", help="provider override.")
    p.set_defaults(func=_rule_add)

    p = rsub.add_parser("list", help="List routing rules (priority order).")
    p.set_defaults(func=_rule_list)

    p = rsub.add_parser("show", help="Show one routing rule (full config read-back).")
    p.add_argument("rule_id", type=bounded_id, help="the rule id.")
    p.set_defaults(func=_rule_show)

    p = rsub.add_parser("toggle", help="Flip a rule active/inactive.")
    p.add_argument("rule_id", type=bounded_id, help="the rule id.")
    p.set_defaults(func=_rule_toggle)

    p = rsub.add_parser("delete", help="Delete a rule (DESTRUCTIVE).")
    p.add_argument("rule_id", type=bounded_id, help="the rule id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_rule_delete)


# --- layer-2 kill switch -----------------------------------------------------
def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("routing")


def _emit_rule_result(emit, d, verb):
    """The shared create/-line, byte-identical to the remote arm's _routing_rule_line."""
    emit.result(
        f"{verb} rule #{d['id']} (prio {d['priority']}, "
        f"ext={d.get('match_ext') or '*'}, dept={d.get('department') or '*'}, "
        f"provider={d.get('provider') or '-'}).",
        {"ok": True, **d},
    )
    return 0


# --- commands ----------------------------------------------------------------
def _rule_add(args, emit) -> int:
    # Reserved-for-v2 fields (subject/workflow/language-set) are consulted by NO reader
    # in v1 -- refuse them before storing dead config, identically local vs --remote and
    # byte-identical to the web form + /api (QW-D trap). Checked BEFORE the remote branch
    # so both arms exit 2 (EXIT_USAGE) with the same message and no needless round trip.
    refuse_reserved_fields(args)
    if remote_mode(args):
        return _remote_impl.routing_rule_add(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    return rule_add_local(args, emit)


def refuse_reserved_fields(args) -> None:
    """Refuse the reserved-for-v2 fields (exit 2) before anything is stored; shared with the
    `admin routing` twin."""
    from app.kernel import routing

    dead = routing.reserved_field_error(
        match_subject=args.subject, workflow_id=args.workflow, language_set_id=args.language_set
    )
    if dead:
        raise CliError(dead, EXIT_USAGE)


def rule_add_local(args, emit) -> int:
    """The in-process create, after the caller's gate (the `routing` feature switch or the
    `admin` operator door). ONE implementation for both command groups (#533)."""
    from app.kernel import routing

    org_id = op_org_id(args)
    with _session() as session:
        # Guardrail parity with the web form + /api: reject a human-review workflow rule
        # AND a rule naming a nonexistent workflow/project/language-set (else a raw FK
        # IntegrityError as a 500 -- #26). Org-scoped: under --org a cross-org referent
        # behaves as nonexistent and the new rule is stamped to the SAME org.
        err = routing.validate_rule(
            session,
            workflow_id=args.workflow,
            project_id=args.project,
            language_set_id=args.language_set,
            org_id=org_id,
        )
        if err:
            raise CliError(err, EXIT_USAGE)
        try:
            rule = routing.create_rule(
                session,
                priority=args.priority,
                match_ext=args.ext,
                match_subject=args.subject,
                department=args.department,
                project_id=args.project,
                workflow_id=args.workflow,
                language_set_id=args.language_set,
                provider=args.provider,
                actor=_actor(args),
                org_id=org_id,
            )
        except routing.RoutingRuleError as e:  # fail-closed backstop, clean not SQL
            raise CliError(str(e), EXIT_USAGE) from e
        return _emit_rule_result(emit, routing.rule_to_dict(rule), "Created")


def _rule_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.routing_rule_list(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    return rule_list_local(args, emit)


def rule_list_local(args, emit) -> int:
    from sqlmodel import select

    from app.kernel import routing
    from app.models import RoutingRule

    # Confine to the resolved --org scope so a scoped operator never sees another org's
    # rules. None (--operator / gate off) = unscoped, byte-identical to today.
    org_id = op_org_id(args)
    with _session() as session:
        stmt = select(RoutingRule)
        if org_id is not None:
            stmt = stmt.where(RoutingRule.org_id == org_id)
        rules = session.exec(stmt.order_by(RoutingRule.priority, RoutingRule.id)).all()
        payload = [routing.rule_to_dict(r) for r in rules]
    if emit.as_json:
        emit.json({"ok": True, "rules": payload})
    else:
        rows = [
            (
                d["id"],
                d["priority"],
                d.get("match_ext") or "*",
                d.get("department") or "*",
                d["project_id"] if d.get("project_id") is not None else "-",
                d.get("provider") or "-",
                "yes" if d.get("active") else "no",
            )
            for d in payload
        ]
        emit.table(rows, headers=("id", "prio", "ext", "dept", "project", "provider", "active"))
        _emit_inert_note(emit, payload)
    return 0


def _emit_inert_note(emit, payload) -> None:
    """Flag any listed rule carrying an authored-but-inert reserved field
    (subject/workflow/language set) so dead config is discoverable in table output
    (QW-D). Table-only; --json consumers already see the fields in rule_to_dict. The
    same note is emitted by the `admin routing rule list` twin + the --remote arm."""
    from app.kernel import routing

    inert = [
        d["id"]
        for d in payload
        if routing.reserved_fields_set(
            match_subject=d.get("match_subject") or "",
            workflow_id=d.get("workflow_id"),
            language_set_id=d.get("language_set_id"),
        )
    ]
    if inert:
        ids = ", ".join(f"#{i}" for i in inert)
        emit.say(
            f"note: rule(s) {ids} carry reserved fields (subject/workflow/language "
            f"set) that are not consulted at run time."
        )


def _rule_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.routing_rule_show(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    return rule_show_local(args, emit)


def rule_show_local(args, emit) -> int:
    from app.kernel import routing

    with _session() as session:
        # Org-scoped read-back: a cross-org / missing id is the same clean "no such rule"
        # (no existence oracle). None (--operator / gate off) = a bare fetch.
        rule = routing.get_rule(session, args.rule_id, org_id=op_org_id(args))
        if rule is None:
            raise CliError(f"no such rule: {args.rule_id}", EXIT_USAGE)
        d = routing.rule_to_dict(rule)
    if emit.as_json:
        emit.json({"ok": True, **d})
    else:
        emit.say(
            f"Rule #{d['id']} (priority {d['priority']}, {'active' if d['active'] else 'inactive'})"
        )
        emit.say(f"  match ext:     {d['match_ext'] or '*'}")
        emit.say(
            f"  match subject: {d['match_subject'] or '*'}{routing.reserved_note(d['match_subject'])}"  # noqa: E501
        )
        emit.say(f"  department:    {d['department'] or '*'}")
        emit.say(f"  project:       {d['project_id'] if d['project_id'] is not None else '-'}")
        emit.say(
            f"  workflow:      {d['workflow_id'] if d['workflow_id'] is not None else '-'}{routing.reserved_note(d['workflow_id'])}"  # noqa: E501
        )
        emit.say(
            f"  language set:  {d['language_set_id'] if d['language_set_id'] is not None else '-'}{routing.reserved_note(d['language_set_id'])}"  # noqa: E501
        )
        emit.say(f"  provider:      {d['provider'] or '-'}")
        emit.say(f"  created by:    {d['created_by'] or '-'}")
        emit.say(f"  created at:    {d['created_at'] or '-'}")
    return 0


def _rule_toggle(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.routing_rule_toggle(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    return rule_toggle_local(args, emit)


def rule_toggle_local(args, emit) -> int:
    from app.kernel import routing

    with _session() as session:
        rule = routing.toggle_rule(
            session, args.rule_id, actor=_actor(args), org_id=op_org_id(args)
        )
        if rule is None:
            raise CliError(f"no such rule: {args.rule_id}", EXIT_USAGE)
        emit.result(
            f"Rule #{args.rule_id}: now {'active' if rule.active else 'inactive'}.",
            {"ok": True, "id": args.rule_id, "active": rule.active},
        )
    return 0


def _rule_delete(args, emit) -> int:
    if remote_mode(args):
        # Confirm LOCALLY before the wire call (the confirm never rides over --remote).
        if not confirm(emit, f"Delete routing rule #{args.rule_id}?", args.yes):
            raise CliError("aborted.", 1)
        return _remote_impl.routing_rule_delete(args, emit)
    _flag_gate()
    require_org_scope(args, emit)
    return rule_delete_local(args, emit)


def rule_delete_local(args, emit) -> int:
    from app.kernel import routing

    if not confirm(emit, f"Delete routing rule #{args.rule_id}?", args.yes):
        raise CliError("aborted.", 1)
    with _session() as session:
        if not routing.delete_rule(
            session, args.rule_id, actor=_actor(args), org_id=op_org_id(args)
        ):
            raise CliError(f"no such rule: {args.rule_id}", EXIT_USAGE)
        emit.result(
            f"Deleted routing rule #{args.rule_id}.",
            {"ok": True, "id": args.rule_id, "deleted": True},
        )
    return 0
