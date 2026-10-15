"""`style-guide` command group: StyleGuide CRUD + approval lifecycle
(SPEC-workflows-redesign Track B, B2).

In-process (local-root, no RBAC — same trust as editing the DB directly) OR --remote
(a thin HTTP client of /api/style-guides, org-scoped ENTIRELY by --token server-side).
Business logic lives in app.services.style_guides; this module is a thin CLI shell.
ASCII-safe output (no em-dashes/emoji — Windows consoles).

Documented in: docs/architecture/style-guides.md
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
    refuse_over_budget,
    remote_mode,
    require_local,
    require_org_scope,
    spend_estimate,
    spend_gate,
)
from ._common import get_customer as _get_customer


def register(subparsers) -> None:
    sgp = subparsers.add_parser(
        "style-guide",
        help="Style guides (long-form prompt assets: create, list, show, update, "
        "set-status, delete).",
    )
    sub = sgp.add_subparsers(dest="style_guide_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "create",
        help="Create a draft style guide (org-global, or "
        "--customer / --content-type / a language pair).",
    )
    p.add_argument("name", help="style guide name.")
    p.add_argument(
        "--customer",
        type=bounded_id,
        default=None,
        help="customer scope id (omit = org-global / applies to everyone).",
    )
    p.add_argument(
        "--content-type",
        type=bounded_id,
        default=None,
        dest="content_type",
        help="content type scope id (omit = any content type).",
    )
    p.add_argument(
        "--source", default="", help="source locale (per-pair guide; needs --target too)."
    )
    p.add_argument(
        "--target", default="", help="target locale (per-pair guide; needs --source too)."
    )
    p.add_argument("--body", default="", help="the guide body text (inline).")
    p.add_argument(
        "--body-file",
        default="",
        dest="body_file",
        help="read the guide body from a file (overrides --body).",
    )
    p.set_defaults(func=_sg_create)

    p = sub.add_parser("list", help="List style guides (org-global + customer-scoped).")
    p.set_defaults(func=_sg_list)

    p = sub.add_parser("show", help="Show one style guide (scope, status, full body).")
    p.add_argument("style_guide_id", type=bounded_id, help="style guide id.")
    p.set_defaults(func=_sg_show)

    p = sub.add_parser(
        "update",
        help="Update a guide's name / body / content-type / "
        "pair (body + scope edits revert approval).",
    )
    p.add_argument("style_guide_id", type=bounded_id, help="style guide id.")
    p.add_argument("--name", default=None, help="new name.")
    p.add_argument("--body", default=None, help="replace the body (inline).")
    p.add_argument(
        "--body-file",
        default=None,
        dest="body_file",
        help="replace the body from a file (overrides --body).",
    )
    p.add_argument(
        "--content-type",
        type=bounded_id,
        default=None,
        dest="content_type",
        help="set the content-type scope id.",
    )
    p.add_argument(
        "--clear-content-type",
        action="store_true",
        dest="clear_content_type",
        help="clear the content-type scope (any content type).",
    )
    p.add_argument("--source", default=None, help="set the pair source locale.")
    p.add_argument("--target", default=None, help="set the pair target locale.")
    p.add_argument(
        "--clear-pair",
        action="store_true",
        dest="clear_pair",
        help="clear the language pair (universal guide).",
    )
    p.set_defaults(func=_sg_update)

    p = sub.add_parser("set-status", help="Set a guide's status (draft|approved).")
    p.add_argument("style_guide_id", type=bounded_id, help="style guide id.")
    p.add_argument("status", choices=("draft", "approved"), help="the new status.")
    p.set_defaults(func=_sg_set_status)

    p = sub.add_parser("delete", help="Delete a style guide (DESTRUCTIVE).")
    p.add_argument("style_guide_id", type=bounded_id, help="style guide id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_sg_delete)

    p = sub.add_parser(
        "ab",
        help="A/B measure whether a style guide improves quality: "
        "run the same segments with the guide ON vs OFF, score both with "
        "the QE judge, print the QE + style-error delta + a verdict. "
        "Numbers, not vibes.",
    )
    p.add_argument("--document", type=bounded_id, required=True, help="document id.")
    p.add_argument(
        "--locale",
        default=None,
        help="target locale (default: the doc's first available target locale).",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="pin an explicit engine for a REAL "
        "measurement (default: the project's per-pair provider; a keyless "
        "install resolves to the offline mock = a structural test only).",
    )
    from app.kernel import context_ab as _ab_svc

    p.add_argument(
        "--max-segments",
        type=int,
        default=None,
        help="cap the number of segments compared (default %d, ceiling %d)."  # noqa: UP031
        % (_ab_svc.DEFAULT_MAX_SEGMENTS, _ab_svc.MAX_SEGMENTS_CEILING),
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm the printed cost estimate and run (nothing is sent without it).",
    )
    p.set_defaults(func=_sg_ab)


# --- helpers -----------------------------------------------------------------
def _visible(session, args):
    """Fetch a style guide in the operator's --org scope, or 404-as-usage.

    #73 COUPLING: op_org_SCOPE — sg.visible now fails closed on a bare None under an
    active gate, so the --operator door hands it access.UNSCOPED to keep cross-org."""
    from app.kernel import style_guides as sg

    g = sg.visible(session, args.style_guide_id, org_id=op_org_scope(args))
    if g is None:
        raise CliError(f"no such style guide: {args.style_guide_id}", EXIT_USAGE)
    return g


def _body_from_args(args, *, default=None):
    """Resolve the body text from --body-file (wins) or --body. Returns `default`
    (None on update = unchanged; "" on create) when neither is given."""
    bf = getattr(args, "body_file", None)
    if bf:
        try:
            with open(bf, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise CliError(f"cannot read {bf}: {exc}", EXIT_USAGE) from exc
    b = getattr(args, "body", None)
    return b if b is not None else default


# --- commands ----------------------------------------------------------------
def _sg_create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.style_guide_create(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import style_guides as sg

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        customer_id = getattr(args, "customer", None)
        if customer_id is not None:
            _get_customer(session, customer_id, op_org_id(args))
        try:
            g = sg.create(
                session,
                args.name,
                # #73 COUPLING: op_org_SCOPE -- sg.create validates the content type via
                # content_types.attachable, which now fails closed on a bare None under an
                # active gate. The --operator door hands access.UNSCOPED so a cross-org
                # create still resolves; sg.create collapses the sentinel to None for the
                # persisted org (org_scope_value). `_get_customer` keeps op_org_id (row
                # guard). --org / gate-off byte-identical.
                org_id=op_org_scope(args),
                customer_id=customer_id,
                content_type_id=getattr(args, "content_type", None),
                source_locale=getattr(args, "source", "") or "",
                target_locale=getattr(args, "target", "") or "",
                body=_body_from_args(args, default="") or "",
                created_by=actor,
            )
        except sg.StyleGuideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action="style_guide.create",
            target=f"style_guide {g.id} '{g.name}'",
            customer_id=g.customer_id,
        )
        emit.result(
            f"Created style guide #{g.id}: {g.name} ({sg.scope_label(g)}) [draft]",
            {"ok": True, **sg.to_dict(session, g)},
        )
    return 0


def _sg_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.style_guide_list(args, emit)
    require_org_scope(args, emit)
    from app.kernel import style_guides as sg

    with _session() as session:
        # #73 COUPLING: op_org_SCOPE so the --operator door (access.UNSCOPED) still lists
        # every org; list_guides now fails closed on a bare None under an active gate.
        guides = sg.list_guides(session, org_id=op_org_scope(args))
        rows, payload = [], []
        for g in guides:
            d = sg.to_dict(session, g, include_body=False)
            rows.append(
                (
                    g.id,
                    g.name,
                    g.status,
                    g.customer_id if g.customer_id else "org-global",
                    d["scope"],
                )
            )
            payload.append(d)
        if emit.as_json:
            emit.json({"ok": True, "style_guides": payload})
        else:
            emit.table(rows, headers=("id", "name", "status", "customer", "scope"))
    return 0


def _sg_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.style_guide_show(args, emit)
    require_org_scope(args, emit)
    from app.kernel import style_guides as sg

    with _session() as session:
        g = _visible(session, args)
        d = sg.to_dict(session, g)
        if emit.as_json:
            emit.json({"ok": True, **d})
        else:
            emit.result(
                f"#{g.id} {g.name} [{g.status}] ({d['scope']})\n"
                f"  approved_by: {g.approved_by or '-'}  body_chars: {d['body_chars']}\n"
                f"  --- body ---\n{g.body or '(empty)'}",
                {"ok": True, **d},
            )
    return 0


def _sg_update(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.style_guide_update(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import style_guides as sg

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _visible(session, args)  # org-scope gate
        gid = args.style_guide_id
        did_anything = False
        try:
            if getattr(args, "name", None) is not None:
                sg.rename(session, gid, args.name)
                did_anything = True
            body = _body_from_args(args, default=None)
            if body is not None:
                sg.set_body(session, gid, body)
                did_anything = True
            change_ct = getattr(args, "content_type", None) is not None or getattr(
                args, "clear_content_type", False
            )
            change_pair = (
                getattr(args, "source", None) is not None
                or getattr(args, "target", None) is not None
                or getattr(args, "clear_pair", False)
            )
            if change_ct or change_pair:
                sg.set_scope(
                    session,
                    gid,
                    content_type_id=getattr(args, "content_type", None),
                    source_locale=(getattr(args, "source", None) or ""),
                    target_locale=(getattr(args, "target", None) or ""),
                    change_content_type=change_ct,
                    change_pair=change_pair,
                )
                did_anything = True
        except sg.StyleGuideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if not did_anything:
            raise CliError(
                "nothing to update (pass --name/--body/--content-type/"
                "--source/--target or a --clear-* flag).",
                EXIT_USAGE,
            )
        g = sg.get(session, gid)
        audit.log(
            actor=actor,
            action="style_guide.update",
            target=f"style_guide {gid}",
            customer_id=g.customer_id,
        )
        emit.result(
            f"Updated style guide #{gid} ({sg.scope_label(g)}) [{g.status}].",
            {"ok": True, **sg.to_dict(session, g)},
        )
    return 0


def _sg_set_status(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.style_guide_set_status(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import style_guides as sg

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        _visible(session, args)
        try:
            g = sg.set_status(session, args.style_guide_id, args.status)
        except sg.StyleGuideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        audit.log(
            actor=actor,
            action=f"style_guide.{args.status}",
            target=f"style_guide {g.id}",
            customer_id=g.customer_id,
        )
        emit.result(
            f"Style guide #{g.id} is now {g.status}.", {"ok": True, **sg.to_dict(session, g)}
        )
    return 0


def _sg_delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.style_guide_delete(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit
    from app.kernel import style_guides as sg

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        g = _visible(session, args)
        if not confirm(emit, f"Delete style guide #{g.id} ({g.name})?", args.yes):
            raise CliError("aborted.", 1)
        sg.delete(session, args.style_guide_id)
        audit.log(
            actor=actor,
            action="style_guide.delete",
            target=f"style_guide {args.style_guide_id}",
            customer_id=g.customer_id,
        )
        emit.result(
            f"Deleted style guide #{args.style_guide_id}.",
            {"ok": True, "style_guide_id": args.style_guide_id, "deleted": True},
        )
    return 0


def _sg_ab(args, emit) -> int:
    """`style-guide ab` — run the style-guide A/B measurement and print the comparison.

    Thin shell over style_ab.run_ab (the ONE service seam web+CLI share). ASCII-safe
    output. Exits 0 on a completed measurement (even a Mock/structural one). Local-only +
    org-scoped, exactly like `context ab`."""
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel import context_ab, style_ab
    from app.kernel.context_ab import ABError
    from app.models import Document, Project

    with _session() as s:
        doc = s.get(Document, args.document)
        if doc is None:
            raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        # Org guard: a foreign org's document is not measurable via this scoped caller.
        org_id = op_org_id(args)
        if org_id is not None:
            proj = s.get(Project, doc.project_id)
            if proj is None or getattr(proj, "org_id", None) != org_id:
                raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        try:
            est = context_ab.estimate_ab(
                s,
                doc,
                locale=args.locale,
                provider=args.provider,
                max_segments=(
                    args.max_segments
                    if args.max_segments is not None
                    else style_ab.DEFAULT_MAX_SEGMENTS
                ),
            )
        except ABError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        refuse_over_budget(doc.project_id, args)
        if not spend_gate(
            emit,
            args,
            spend_estimate(
                est["provider"],
                est["model"],
                calls=est["calls"],
                input_tokens=est["input_tokens"],
                output_tokens=est["output_tokens"],
                note=f"{est['segments']} segment(s) x 4 calls (translate ON/OFF + QE judge each).",
            ),
            verb="style-guide ab",
        ):
            return 0
        try:
            res = style_ab.run_ab(
                s,
                doc,
                locale=args.locale,
                provider=args.provider,
                max_segments=(
                    args.max_segments
                    if args.max_segments is not None
                    else style_ab.DEFAULT_MAX_SEGMENTS
                ),
                org_id=org_id,
            )
        except ABError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        data = res.as_dict()
    if emit.as_json:
        emit.json({"ok": True, "ab": data})
        return 0
    emit.say(
        "Style-guide A/B measurement -- document #{} ({})".format(
            data["document_id"], data["locale"]
        )
    )
    emit.say(
        "  provider: %s%s | judge: %s | N=%d of %d translatable (cap %d)"  # noqa: UP031
        % (
            data["provider"],
            (" / " + data["model"]) if data["model"] else "",
            data["qe_provider"],
            data["n"],
            data["n_available"],
            data["max_segments"],
        )
    )
    emit.say("  mode: {}".format(data["label"]))
    if data["guide_present"]:
        emit.say("  guide: #{} {}".format(data["guide_id"], data["guide_name"]))
    emit.say("")
    if not data["guide_present"]:
        emit.say("  (no APPROVED style guide resolved -- both arms identical)")
    on, off, delta = data["on"], data["off"], data["delta"]

    def _fmt(v, suffix=""):
        return "n/a" if v is None else (f"{v}{suffix}")

    emit.say("  Overall QE (mean, 0-100):")
    emit.say(
        "    guide ON : %s  (scored %d, skipped %d)"  # noqa: UP031
        % (_fmt(on["mean_qe"]), on["scored"], on["skipped"])
    )
    emit.say(
        "    guide OFF: %s  (scored %d, skipped %d)"  # noqa: UP031
        % (_fmt(off["mean_qe"]), off["scored"], off["skipped"])
    )
    emit.say("    delta     : {}".format(_fmt(delta["mean_qe"])))
    emit.say("")
    emit.say("  Style errors (QE judge 'style' category, mean per segment; LOWER=better):")
    emit.say("    guide ON : %s  (%d total)" % (_fmt(on["mean_style_errors"]), on["style_errors"]))  # noqa: UP031
    emit.say(
        "    guide OFF: %s  (%d total)" % (_fmt(off["mean_style_errors"]), off["style_errors"])  # noqa: UP031
    )
    emit.say("    delta     : {}".format(_fmt(delta["mean_style_errors"])))
    emit.say("")
    emit.say("  VERDICT: {}".format(data["verdict"]))
    return 0
