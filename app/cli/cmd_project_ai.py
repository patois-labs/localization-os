"""cmd_project: AI opt-out verbs (HECVAT DPAI-08).

`project set-ai <id> off|on` and `customer set-ai <id> off|on`. "off" keeps the content
of the project (or of every project of the customer) away from this instance's AI engines;
"on" switches AI back on. In-process only, like `project archive`: the
/api/projects/{id}/ai-opt-out and /api/customers/{id}/ai-opt-out routes serve the web and
remote-API surfaces. Audited by the service (project.ai_opt_out / project.ai_opt_in,
customer.ai_opt_out / customer.ai_opt_in). The CLI is the trusted local operator.

Documented in: docs/architecture/content-and-projects.md
"""

from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _session,
    op_org_id,
    op_org_scope,
    require_local,
    require_org_scope,
)
from app.cli.cmd_project_project import _get_customer, _get_project

AI_CHOICES = ("off", "on")


def _project_set_ai(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import ai_optout

    actor = getattr(args, "actor", None) or "cli"
    value = args.state == "off"  # AI off == opted out
    with _session() as session:
        _get_project(session, args.project_id, op_org_id(args))
        # op_org_scope, not op_org_id: the #73 vocabulary, so --operator (UNSCOPED)
        # still writes cross-org while a bare None under an active gate fails closed.
        project = ai_optout.set_project(
            session, args.project_id, value, actor=actor, org_id=op_org_scope(args)
        )
        if project is None:
            raise CliError(f"no such project: {args.project_id}", EXIT_USAGE)
        st = ai_optout.status(session, project)
        if value:
            msg = (
                f"AI is now OFF for project #{project.id} ({project.name}). "
                "This instance's AI engines never process this project's content. "
                "Content you download, export, or connect to your own AI tools "
                "(for example the MCP agent connection) is outside this switch."
            )
        elif st["ai_opt_out"]:
            msg = (
                f"The project switch is off for #{project.id} ({project.name}), but AI stays "
                f"OFF because its customer opted out (use `customer set-ai`)."
            )
        else:
            msg = f"AI is back ON for project #{project.id} ({project.name})."
        emit.result(msg, {"ok": True, "id": project.id, "name": project.name, **st})
    return 0


def _customer_set_ai(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import ai_optout

    actor = getattr(args, "actor", None) or "cli"
    value = args.state == "off"
    with _session() as session:
        _get_customer(session, args.customer_id, op_org_id(args))
        cust = ai_optout.set_customer(
            session, args.customer_id, value, actor=actor, org_id=op_org_scope(args)
        )
        if cust is None:
            raise CliError(f"no such customer: {args.customer_id}", EXIT_USAGE)
        msg = (
            f"AI is now OFF for every project of customer #{cust.id} ({cust.name})."
            if value
            else f"The customer-level AI opt-out is off for #{cust.id} ({cust.name}); "
            f"project-level opt-outs still apply."
        )
        emit.result(
            msg, {"ok": True, "id": cust.id, "name": cust.name, "ai_opt_out": bool(cust.ai_opt_out)}
        )
    return 0


COMMENT_CHOICES = ("on", "off")


def _project_set_comments(args, emit) -> int:
    """#690: `project set-comments <id> on|off` - translate comments and annotations in
    Office files (Word, Excel, PowerPoint, OpenDocument, Visio) or keep them in the
    source language. In-process, like `project set-ai`; the
    /api/projects/{id}/translate-comments route serves the web and remote-API surfaces.
    Audited by the service."""
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import comment_option

    actor = getattr(args, "actor", None) or "cli"
    value = args.state == "on"
    with _session() as session:
        project = _get_project(session, args.project_id, op_org_id(args))
        st = comment_option.set_for(project, value, actor=actor)
        msg = (
            f"Comments in Office files are now TRANSLATED for project #{project.id} "
            f"({project.name}): Word, Excel, PowerPoint, OpenDocument and Visio. Authors and "
            "dates never change. Applies to files uploaded or re-imported from now on."
            if value
            else f"Comments in Office files now stay in the source language for project "
            f"#{project.id} ({project.name}). Applies to files uploaded or re-imported from now on."
        )
        emit.result(msg, {"ok": True, "id": project.id, "name": project.name, **st})
    return 0
