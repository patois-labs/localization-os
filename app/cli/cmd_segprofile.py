"""`segprofile ...` - segmentation profiles (P6, item 8), the CLI half of the
/settings "Segmentation profiles" section + the per-project override picker.

In-process, local-root (same trust as editing the DB directly). Business logic
lives in app.services.segmentation_profiles - this is a thin shell: parse args,
call the service, emit a table (or --json). Rulesets are immutable (clone-before-
edit, the memoQ convention); `create --file` is bounded + parse-validated exactly
like the web upload. `assign` sets/clears a project's override (the "untick
default segmentation" pick); `set-default` marks the single system-default rung.

Documented in: docs/architecture/segmentation.md
"""

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    op_org_id,
    remote_mode,
    require_org_scope,
)


def register(subparsers) -> None:
    sp = subparsers.add_parser("segprofile", help="Segmentation profiles (named SRX rulesets).")
    sub = sp.add_subparsers(dest="segprofile_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("list", help="List segmentation profiles.")
    p.set_defaults(func=_list)

    p = sub.add_parser("show", help="Show one profile (metadata; blobs never echo).")
    p.add_argument("profile_id", type=bounded_id)
    p.set_defaults(func=_show)

    p = sub.add_parser(
        "create",
        help="Create a profile (built-in rules, or --file "
        "for a custom SRX - validated before it is accepted).",
    )
    p.add_argument("name", help="unique profile name.")
    p.add_argument(
        "--file",
        default="",
        help="path to a .srx ruleset (max 1MB); omit to use the built-in default rules.",
    )
    p.add_argument("--customer-id", default=None, help="scope to one customer (omit = global).")
    p.add_argument("--description", default="")
    p.add_argument(
        "--system-default",
        action="store_true",
        help="make this the system default (rung 3 of the chain).",
    )
    p.set_defaults(func=_create)

    p = sub.add_parser(
        "clone",
        help="Clone a profile's ruleset under a new name "
        "(rulesets are immutable - clone before editing).",
    )
    p.add_argument("profile_id", type=bounded_id)
    p.add_argument("new_name")
    p.add_argument(
        "--customer-id",
        default=None,
        help="re-scope the clone (omit = keep the source's scope; '' = global).",
    )
    p.set_defaults(func=_clone)

    p = sub.add_parser("rename", help="Rename a segmentation profile.")
    p.add_argument("profile_id", type=bounded_id)
    p.add_argument("name", help="the new name.")
    p.set_defaults(func=_rename)

    p = sub.add_parser(
        "delete", help="Delete a profile (refused while any project/customer references it)."
    )
    p.add_argument("profile_id", type=bounded_id)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_delete)

    p = sub.add_parser("set-default", help="Mark (or clear) the system-default profile.")
    p.add_argument("profile_id", type=bounded_id)
    p.add_argument("--clear", action="store_true", help="clear instead of set.")
    p.set_defaults(func=_set_default)

    p = sub.add_parser(
        "assign",
        help="Set or clear a project's segmentation "
        "override (blank profile = back to the inherit chain).",
    )
    p.add_argument("project_id", type=bounded_id)
    p.add_argument("--profile", default="", help="profile id (omit/blank = clear the override).")
    p.set_defaults(func=_assign)


def _svc():
    from app.kernel import segmentation_profiles

    return segmentation_profiles


def _list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_list(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    rows = [svc.profile_row(p) for p in svc.list_profiles(org_id=op_org_id(args))]
    if emit.as_json:
        emit.json({"ok": True, "profiles": rows})
        return 0
    if not rows:
        emit.say("No segmentation profiles. Projects segment with the active ruleset.")
        return 0
    emit.table(
        [
            (
                r["id"],
                r["name"],
                r["scope"],
                "custom" if r["custom"] else "built-in",
                "yes" if r["is_system_default"] else "",
            )
            for r in rows
        ],
        headers=("id", "name", "scope", "rules", "default"),
    )
    return 0


def _show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_show(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    prof = svc.get_profile(args.profile_id, org_id=op_org_id(args))
    if prof is None:
        raise CliError(f"no such segmentation profile: {args.profile_id}")
    row = svc.profile_row(prof)
    use = svc.in_use_count(prof.id)
    emit.result(
        f"#{row['id']} {row['name']} [{row['scope']}] "
        f"{'custom SRX' if row['custom'] else 'built-in rules'}"
        f"{' (system default)' if row['is_system_default'] else ''}\n"
        f"  description: {row['description'] or '(none)'}\n"
        f"  in use: {use['projects']} project(s), {use['customers']} customer(s)",
        {"ok": True, "profile": row, "in_use": use},
    )
    return 0


def _create(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_create(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    blob = None
    if (args.file or "").strip():
        try:
            with open(args.file, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise CliError(f"cannot read {args.file}: {exc}", EXIT_USAGE) from exc
        ok, msg = svc.validate_ruleset(raw)
        if not ok:
            raise CliError(f"rejected: {msg}", EXIT_USAGE)
        blob = raw.decode("utf-8", errors="replace")
    try:
        prof = svc.create_profile(
            args.name,
            srx_blob=blob,
            customer_id=args.customer_id,
            description=args.description,
            is_system_default=bool(args.system_default),
            actor=getattr(args, "actor", None) or "cli",
            org_id=op_org_id(args),
        )
    except svc.SegProfileError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(
        f"Created segmentation profile #{prof.id}: {prof.name}.",
        {"ok": True, "profile": svc.profile_row(prof)},
    )
    return 0


def _clone(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_clone(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    try:
        prof = svc.clone_profile(
            args.profile_id,
            args.new_name,
            customer_id=args.customer_id,
            actor=getattr(args, "actor", None) or "cli",
            org_id=op_org_id(args),
        )
    except svc.SegProfileError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(
        f"Cloned to #{prof.id}: {prof.name}.", {"ok": True, "profile": svc.profile_row(prof)}
    )
    return 0


def _delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_delete(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    oid = op_org_id(args)
    prof = svc.get_profile(args.profile_id, org_id=oid)
    if prof is None:
        raise CliError(f"no such segmentation profile: {args.profile_id}")
    use = svc.in_use_count(prof.id)
    if use["total"]:
        raise CliError(
            f"profile is in use by {use['projects']} project(s) and "
            f"{use['customers']} customer(s) - reassign them first."
        )
    if not confirm(
        emit, f"Delete segmentation profile '{prof.name}'?", getattr(args, "yes", False)
    ):
        return EXIT_USAGE
    # The service now ALSO fails closed (SegProfileError) — surface any race as a
    # clean CLI error, never a traceback.
    try:
        svc.delete_profile(prof.id, org_id=oid)
    except svc.SegProfileError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(f"Deleted segmentation profile #{prof.id}.", {"ok": True, "deleted": prof.id})
    return 0


def _rename(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_rename(args, emit)
    require_org_scope(args, emit)
    from app.kernel import audit

    svc = _svc()
    oid = op_org_id(args)
    prof = svc.get_profile(args.profile_id, org_id=oid)
    if prof is None:
        raise CliError(f"no such segmentation profile: {args.profile_id}")
    old_name = prof.name
    try:
        prof = svc.update_profile(args.profile_id, name=args.name, org_id=oid)
    except svc.SegProfileError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    if prof is None:
        raise CliError(f"no such segmentation profile: {args.profile_id}")
    audit.log(
        actor=getattr(args, "actor", None) or "cli",
        action="segprofile.rename",
        target=f"profile {prof.id}",
        detail=f"{old_name!r} -> {prof.name!r}",
    )
    emit.result(
        f"Renamed segmentation profile #{prof.id} to {prof.name}.",
        {"ok": True, "profile": svc.profile_row(prof)},
    )
    return 0


def _set_default(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_set_default(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    try:
        prof = svc.update_profile(
            args.profile_id, is_system_default=not args.clear, org_id=op_org_id(args)
        )
    except svc.SegProfileError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    if prof is None:
        raise CliError(f"no such segmentation profile: {args.profile_id}")
    emit.result(
        ("Cleared system default from " if args.clear else "System default set to ")
        + f"#{prof.id} ({prof.name}).",
        {"ok": True, "profile": svc.profile_row(prof)},
    )
    return 0


def _assign(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.segprofile_assign(args, emit)
    require_org_scope(args, emit)
    svc = _svc()
    with _session() as session:
        try:
            proj = svc.assign_to_project(
                session, args.project_id, args.profile or "", org_id=op_org_id(args)
            )
        except svc.SegProfileError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if proj is None:
            raise CliError(f"no such project: {args.project_id}")
        tgt = proj.segmentation_profile_id
    emit.result(
        f"Project #{proj.id} segmentation override: "
        + (f"profile #{tgt}." if tgt else "cleared (inherit chain)."),
        {"ok": True, "project_id": proj.id, "segmentation_profile_id": tgt},
    )
    return 0
