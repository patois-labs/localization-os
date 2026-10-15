"""`keys` command registration and key CRUD, translation, and image CLI verbs.

SPEC-key-based-projects section 7. These thin service shells retain parity with
the /projects/<id>/keys web grid and the `/api/keys` group.

Shared project/actor/label resolution and feature gating live in `keys_common`.
Import/export, delivery gates, and the cursor-file change feed live in
`keys_import_export`. This module composes their parsers and re-exports moved
symbols for existing callers. Every verb retains full `--remote` parity and
refuses cleanly when `key_projects_enabled` is off. ASCII-only output supports
Windows consoles.

Documented in: docs/architecture/string-keys.md
"""

# Transitional shim: preserve imports from cmd_keys while ownership moves.
from app.cli.keys_common import (
    _actor,
    _clip,
    _flag_gate,
    _label,
    _project,
    _project_arg,
    _resolve_key,
)
from app.cli.keys_import_export import (
    DEFAULT_CURSOR_FILE,
    _changes_mode_guard,
    _export_payload,
    _keys_changes,
    _keys_deliver_approve,
    _keys_deliver_revoke,
    _keys_empties,
    _keys_export,
    _keys_import,
    emit_changes,
    emit_export,
    emit_import_report,
    persist_cursor,
    read_cursor_file,
    register_changes,
    register_import_export,
    resolve_cursor,
    write_cursor_file,
)

from . import _remote_impl
from ._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    get_scoped_or_404,
    op_org_id,
    remote_mode,
    require_org_scope,
)


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "keys",
        help="Key-based projects: add, list, set, delete, import, "
        "export string keys and poll the change feed.",
    )
    sub = grp.add_subparsers(dest="keys_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("add", help="Create a string key (optionally with its base value).")
    _project_arg(p)
    p.add_argument(
        "--name", required=True, help="the key name ('::' separates nested JSON path segments)."
    )
    p.add_argument("--namespace", default="", help="namespace (default: the empty one).")
    p.add_argument("--context", default="", help="disambiguator for two keys that share a name.")
    p.add_argument("--description", default="", help="translator-facing note.")
    p.add_argument(
        "--base",
        default="",
        help="the source-locale value (on a plural key it fills the 'other' form ONLY).",
    )
    p.add_argument(
        "--plural",
        action="store_true",
        help="make this a plural key (materializes each locale's CLDR "
        "forms; --base fills 'other' only -- set the remaining forms "
        "with `keys set --locale <source> --form <form>`).",
    )
    p.add_argument("--tag", action="append", default=[], dest="tags", help="a tag (repeatable).")
    p.add_argument(
        "--char-limit", type=int, default=None, help="maximum characters for the translation."
    )
    p.set_defaults(func=_keys_add)

    p = sub.add_parser("list", help="List a project's keys with their per-locale values.")
    _project_arg(p)
    p.add_argument("--namespace", default=None, help="only this namespace.")
    p.add_argument("--tag", default="", help="only keys carrying this tag.")
    p.add_argument("--search", default="", help="substring of name/description/context.")
    p.add_argument(
        "--status",
        default="",
        help="only keys with a value in this "
        "segment status (new, mt, edited, needs_review, approved).",
    )
    p.add_argument(
        "--untranslated-in",
        default="",
        dest="untranslated_in",
        help="only keys with an empty value in this locale.",
    )
    p.add_argument(
        "--archived", action="store_true", help="include archived keys (excluded by default)."
    )
    p.add_argument(
        "--include-deleted",
        action="store_true",
        dest="include_deleted",
        help="also show SOFT-DELETED keys (parity with the grid's "
        "'include deleted' filter). Use it to find a key to restore.",
    )
    p.add_argument("--limit", type=int, default=100, help="page size (default 100).")
    p.add_argument(
        "--cursor",
        type=int,
        default=0,
        help="resume after this key id (see next_cursor in --json).",
    )
    p.set_defaults(func=_keys_list)

    p = sub.add_parser("set", help="Set one locale's value for a key.")
    _project_arg(p)
    p.add_argument("--name", required=True, help="the key name.")
    p.add_argument("--namespace", default="", help="the key's namespace.")
    p.add_argument("--context", default="", help="the key's context.")
    p.add_argument(
        "--locale",
        required=True,
        help="the target locale, or the project's source locale to set the BASE value.",
    )
    p.add_argument(
        "--form", default="", help="CLDR plural form (one, other, ...) for a plural key."
    )
    p.add_argument("--text", required=True, help="the value.")
    p.set_defaults(func=_keys_set)

    p = sub.add_parser(
        "delete",
        help="Soft-delete a key (tombstoned in the change feed; restorable in the web grid).",
    )
    _project_arg(p)
    p.add_argument("--name", required=True, help="the key name.")
    p.add_argument("--namespace", default="", help="the key's namespace.")
    p.add_argument("--context", default="", help="the key's context.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation.")
    p.set_defaults(func=_keys_delete)

    p = sub.add_parser(
        "restore",
        help="Undo a soft delete (the inverse of `keys delete`; the grid's Restore button).",
    )
    _project_arg(p)
    p.add_argument("--name", required=True, help="the key name.")
    p.add_argument("--namespace", default="", help="the key's namespace.")
    p.add_argument("--context", default="", help="the key's context.")
    p.set_defaults(func=_keys_restore)

    p = sub.add_parser(
        "edit",
        help="Edit a key: metadata, identity (rename / move namespace) and the plural setting.",
    )
    _project_arg(p)
    p.add_argument("--name", required=True, help="the key name (which key to edit).")
    p.add_argument("--namespace", default="", help="the key's namespace.")
    p.add_argument("--context", default="", help="the key's context.")
    p.add_argument(
        "--rename",
        default=None,
        dest="new_name",
        help="give the key a NEW name (same namespace + context).",
    )
    p.add_argument(
        "--move-namespace",
        default=None,
        dest="new_namespace",
        help="move the key to another namespace (its value rows move with it).",
    )
    p.add_argument(
        "--set-context",
        default=None,
        dest="new_context",
        help="change the key's context (part of its identity).",
    )
    p.add_argument("--description", default=None, help="translator-facing note.")
    p.add_argument(
        "--tags", default=None, help="REPLACE the tag list, comma-separated ('' clears it)."
    )
    p.add_argument(
        "--char-limit",
        default=None,
        dest="char_limit",
        help="maximum characters for the translation ('' clears it).",
    )
    p.add_argument(
        "--archive",
        dest="archive",
        action="store_const",
        const=True,
        default=None,
        help="retire the key from new build files.",
    )
    p.add_argument(
        "--unarchive",
        dest="archive",
        action="store_const",
        const=False,
        help="put an archived key back into the build.",
    )
    p.add_argument(
        "--hide",
        dest="hide",
        action="store_const",
        const=True,
        default=None,
        help="hide the key from translators.",
    )
    p.add_argument(
        "--unhide",
        dest="hide",
        action="store_const",
        const=False,
        help="show the key to translators again.",
    )
    p.add_argument(
        "--plural",
        dest="plural",
        action="store_const",
        const=True,
        default=None,
        help="promote to a plural key (materializes each locale's CLDR forms).",
    )
    p.add_argument(
        "--single",
        dest="plural",
        action="store_const",
        const=False,
        help="demote to a single value. REFUSED while another form still "
        "holds a translation, unless --force.",
    )
    p.add_argument(
        "--plural-arg", default=None, dest="plural_arg_name", help="the plural argument name."
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="accept the loss when a demotion would discard translated plural forms.",
    )
    p.set_defaults(func=_keys_edit)

    register_import_export(sub)

    p = sub.add_parser(
        "translate",
        help="Launch translation over a key project for "
        "one target locale (or --locale all), through a workflow.",
    )
    _project_arg(p)
    p.add_argument(
        "--workflow",
        required=True,
        type=bounded_id,
        dest="workflow_id",
        help="the workflow to run (its id).",
    )
    p.add_argument(
        "--locale",
        required=True,
        help="a target locale, or 'all' to fan out across every active "
        "target locale on the project.",
    )
    p.set_defaults(func=_keys_translate)

    register_changes(sub)

    # --- per-string in-context images (Batch 14 T1) ---
    ip = sub.add_parser(
        "image",
        help="Attach / list / remove in-context images on a string key (for vision engines).",
    )
    isub = ip.add_subparsers(dest="keys_image_cmd", metavar="<subcommand>")
    isub.required = True

    p = isub.add_parser("add", help="Attach a PNG or JPEG in-context image to a string key.")
    p.add_argument("key_id", type=bounded_id, help="the string key's id.")
    p.add_argument("path", help="path to a PNG or JPEG image on this machine.")
    p.add_argument("--caption", default="", help="an optional translator-facing note.")
    p.set_defaults(func=_keys_image_add)

    p = isub.add_parser("list", help="List a string key's in-context images.")
    p.add_argument("key_id", type=bounded_id, help="the string key's id.")
    p.set_defaults(func=_keys_image_list)

    p = isub.add_parser("rm", help="Soft-delete one of a string key's images.")
    p.add_argument("key_id", type=bounded_id, help="the string key's id.")
    p.add_argument("image_id", type=bounded_id, help="the image's id (from `keys image list`).")
    p.set_defaults(func=_keys_image_rm)

    p = isub.add_parser(
        "set-primary", help="Make one image the key's primary (the one sent by default)."
    )
    p.add_argument("key_id", type=bounded_id, help="the string key's id.")
    p.add_argument("image_id", type=bounded_id, help="the image's id (from `keys image list`).")
    p.set_defaults(func=_keys_image_set_primary)


def key_rows(views, locales):
    """The human table both the local and the --remote arm print, byte-identically."""
    rows = []
    for v in views:
        cells = []
        for loc in locales:
            vals = v.get("values", {}).get(loc) or []
            # K8: a compact "!" on an over-limit cell (spec section 8, parity law -- no new
            # column). `over_limit` is the DERIVED marker key_view attaches; a footer in
            # emit_key_list explains it. ASCII, so it is safe on a Windows console.
            cells.append(
                " | ".join(
                    (_clip(x.get("text") or "", 18) + ("!" if x.get("over_limit") else ""))
                    for x in vals
                )
                or "-"
            )
        base = " | ".join(_clip(t, 18) for t in (v.get("base") or {}).values()) or "-"
        rows.append((v["id"], v.get("namespace") or "(default)", v["name"], base, *cells))
    return rows


def locales_of(views) -> list:
    """Every locale any of these keys carries a value in, in stable order."""
    seen = []
    for v in views:
        for loc in v.get("values") or {}:
            if loc not in seen:
                seen.append(loc)
    return sorted(seen)


def emit_key_list(emit, payload) -> None:
    """The shared renderer for `keys list` (local + --remote)."""
    views = payload.get("keys") or []
    if emit.as_json:
        emit.json({"ok": True, **payload})
        return
    locales = locales_of(views)
    emit.table(key_rows(views, locales), headers=("id", "namespace", "name", "base", *locales))
    over = [
        v
        for v in views
        if any(x.get("over_limit") for vals in (v.get("values") or {}).values() for x in vals)
    ]
    if over:
        emit.say(
            "! = value exceeds the key's character limit (%d key(s): %s)"  # noqa: UP031
            % (len(over), ", ".join(_label(v.get("namespace"), v.get("name")) for v in over[:8]))
        )
    if payload.get("has_more"):
        emit.say("more keys available: re-run with --cursor {}".format(payload.get("next_cursor")))


# --- verbs -------------------------------------------------------------------
def _keys_add(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_add(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    with _session() as session:
        project = _project(session, args)
        from app.kernel import connected_projects

        if connected_projects.is_connected(project):
            raise CliError(connected_projects.UPLOAD_REFUSED, EXIT_USAGE)
        base = args.base if args.base.strip() else None
        try:
            key = keys_svc.create_key(
                session,
                project,
                args.name,
                namespace=args.namespace,
                context=args.context,
                description=args.description,
                is_plural=bool(args.plural),
                char_limit=args.char_limit,
                tags=list(args.tags) or None,
                base_value=({"other": base} if (base is not None and args.plural) else base),
                # `--base` is ONE string, and on a plural key it fills `other` -- which
                # is what its help says (live-QA K5). Fanning that singular text across
                # one/few/many gave every plural form the wrong SOURCE text, and MT
                # then translated a singular source into every slot.
                base_fill_missing=not args.plural,
                actor=_actor(args),
            )
        except keys_svc.KeyServiceError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # `--base` on a plural key fills `other` ONLY, which is what its help has
        # always said (live-QA K5) -- so SAY which forms are still without source
        # text, rather than leaving the user to discover it after MT has translated
        # a singular string into every slot.
        hint = _plural_base_hint(session, project, key) if args.plural else ""
        payload = {"ok": True, "key": keys_svc.key_view(session, key)}
        if hint:
            payload["hint"] = hint
        emit.result(
            "Added key #%d: %s" % (key.id, _label(key.namespace, key.name, key.context)),  # noqa: UP031
            payload,  # noqa: RUF100, UP031
        )
        if hint:
            emit.say("  " + hint)
    return 0


def _plural_base_hint(session, project, key) -> str:
    """The one-line nudge for a plural key whose non-`other` forms have no source
    text yet. "" when there is nothing to say (no base value, or every form filled)."""
    from app.kernel import keys as keys_svc

    base = keys_svc.base_values(session, key)
    if not (base.get("other") or "").strip():
        return ""
    missing = [c for c, v in base.items() if c and c != "other" and not (v or "").strip()]
    if not missing:
        return ""
    return (
        "--base filled the 'other' form only; the {} form(s) still need their own "
        "source text (`keys set --project {} --name {} --locale {} --form <form> "
        "--text ...`).".format(", ".join(missing), project.id, key.name, project.source_locale)
    )


def _keys_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_list(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    with _session() as session:
        project = _project(session, args)
        page = keys_svc.page_keys(
            session,
            project.id,
            cursor=args.cursor,
            limit=args.limit,
            namespace=args.namespace,
            tag=(args.tag or None),
            q=(args.search or None),
            status=(args.status or None),
            untranslated_in=(args.untranslated_in or None),
            include_archived=bool(args.archived),
            include_deleted=bool(getattr(args, "include_deleted", False)),
        )
        emit_key_list(
            emit,
            {
                "project_id": project.id,
                "keys": keys_svc.key_views(session, page["keys"], project_id=project.id),
                "next_cursor": page["next_cursor"],
                "has_more": page["has_more"],
            },
        )
    return 0


def _keys_set(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_set(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    with _session() as session:
        project = _project(session, args)
        key = _resolve_key(session, project, args)
        try:
            if args.locale == project.source_locale:
                # The SOURCE locale means the BASE value (auto-unverify applies) --
                # the same rule import/export follow, so one flag means one thing.
                base = keys_svc.base_values(session, key)
                value = {**base, (args.form or "other"): args.text} if key.is_plural else args.text
                # `fill_missing=False` because this map is COMPLETE and MEANS IT
                # (fix-round F6). It merges the stored base map, which holds "" for
                # every form `keys add --plural --base` deliberately left empty -- and
                # "" is falsy, so the fallback in `_source_for` treated a named-but-
                # empty form as unnamed and re-fanned the singular `other` string
                # across all four. That is the K5 defect, re-entered by the very
                # command K5's own hint tells the user to run, and it dragged
                # auto-unverify along with it. A form this caller names as empty is
                # empty. (A form no ACTIVE locale has a row for yet is born without
                # source text rather than inheriting `other`; the plural-base hint
                # names it, which is the same nudge K5 added.)
                keys_svc.set_base_value(session, key, value, actor=_actor(args), fill_missing=False)
            else:
                keys_svc.set_value(
                    session,
                    key,
                    args.locale,
                    args.text,
                    origin=keys_svc.ORIGIN_HUMAN,
                    plural_category=(args.form or None),
                    actor=_actor(args),
                )
        except keys_svc.KeyServiceError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            "Set {} [{}{}].".format(
                _label(key.namespace, key.name), args.locale, "/" + args.form if args.form else ""
            ),
            {"ok": True, "key": keys_svc.key_view(session, key)},
        )
    return 0


def _keys_delete(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_delete(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    with _session() as session:
        project = _project(session, args)
        key = _resolve_key(session, project, args)
        if not confirm(
            emit,
            f"Soft-delete key {_label(key.namespace, key.name, key.context)}?",
            args.yes,
        ):
            raise CliError("aborted.", EXIT_USAGE)
        keys_svc.delete_key(session, key, actor=_actor(args))
        emit.result(
            f"Deleted {_label(key.namespace, key.name, key.context)} (tombstoned; restorable).",
            {"ok": True, "id": key.id, "deleted": True},
        )
    return 0


def _keys_restore(args, emit) -> int:
    """Undo a soft delete. Restore used to be WEB-ONLY, which made the headless story
    incomplete in the one place it hurts: `keys import --cleanup` is a HEADLESS verb
    that soft-deletes keys, so the operator most likely to need a restore was the one
    who had no way to run it."""
    if remote_mode(args):
        return _remote_impl.keys_restore(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    with _session() as session:
        project = _project(session, args)
        key = keys_svc.get_key(
            session,
            project.id,
            args.name,
            args.namespace or "",
            args.context or "",
            include_deleted=True,
        )
        if key is None:
            raise CliError(
                f"no such key: {_label(args.namespace, args.name, args.context)}",
                EXIT_USAGE,
            )
        if key.deleted_at is None:
            raise CliError(
                f"{_label(key.namespace, key.name, key.context)} is not deleted.",
                EXIT_USAGE,
            )
        try:
            keys_svc.restore_key(session, key, actor=_actor(args))
        except keys_svc.KeyServiceError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Restored {_label(key.namespace, key.name, key.context)}.",
            {"ok": True, "id": key.id, "deleted": False, "key": keys_svc.key_view(session, key)},
        )
    return 0


def edit_fields(args) -> dict:
    """The `keys edit` flags as `keys.apply_key_edit` kwargs -- shared by BOTH arms
    so the local and `--remote` halves cannot accept different edits. Only flags the
    operator actually passed appear (argparse defaults them to None), so an omitted
    flag never clears a field."""
    out: dict = {}
    if args.new_name is not None:
        out["name"] = args.new_name
    if args.new_namespace is not None:
        out["namespace"] = args.new_namespace
    if args.new_context is not None:
        out["context"] = args.new_context
    if args.description is not None:
        out["description"] = args.description
    if args.tags is not None:
        out["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.char_limit is not None:
        raw = str(args.char_limit).strip()
        if raw:
            try:
                out["char_limit"] = int(raw)
            except ValueError:
                raise CliError("--char-limit must be a whole number.", EXIT_USAGE) from None
        else:
            out["char_limit"] = None
    if args.archive is not None:
        out["is_archived"] = bool(args.archive)
    if args.hide is not None:
        out["is_hidden"] = bool(args.hide)
    if args.plural is not None:
        out["is_plural"] = bool(args.plural)
    if args.plural_arg_name is not None:
        out["plural_arg_name"] = args.plural_arg_name
    if args.force:
        out["force"] = True
    return out


def _keys_edit(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_edit(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    fields = edit_fields(args)
    if not fields or set(fields) == {"force"}:
        raise CliError(
            "nothing to change: pass at least one of --rename, "
            "--move-namespace, --set-context, --description, --tags, "
            "--char-limit, --archive/--unarchive, --hide/--unhide, "
            "--plural/--single, --plural-arg.",
            EXIT_USAGE,
        )
    with _session() as session:
        project = _project(session, args)
        key = _resolve_key(session, project, args)
        try:
            keys_svc.apply_key_edit(session, key, actor=_actor(args), **fields)
        except keys_svc.KeyServiceError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Updated {_label(key.namespace, key.name, key.context)}.",
            {"ok": True, "key": keys_svc.key_view(session, key)},
        )
    return 0


def _keys_translate(args, emit) -> int:
    """Launch translation over a key project (Q8 parity: the /api route existed, the CLI
    had no translate verb). Wraps the SHARED seam keys_svc.launch_translation -- the same
    one POST /api/projects/{id}/keys/translate calls -- so the loop lives in ONE place.
    SYNCHRONOUS in-process (P-A2): passes wait=True so the seam BLOCKS until every run it
    launched is terminal (or human-review paused) BEFORE returning. A one-shot CLI process
    kills start_run's daemon run thread the instant it exits, so an async launch here would
    be a false success -- "Started" printed while the run is stuck RUNNING with zero
    translations. The /api route keeps wait=False (async-launch, never blocks the event
    loop -- Q2). The --remote arm hits the /api route and polls the run itself."""
    if remote_mode(args):
        return _remote_impl.keys_translate(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import content as content_svc
    from app.kernel import keys as keys_svc
    from app.kernel import orchestrator
    from app.kernel import workflows as wf_service

    with _session() as session:
        project = _project(session, args)
        wf = _resolve_workflow(session, args)
        try:
            outcome = keys_svc.launch_translation(
                session, project, wf.id, args.locale, actor=_actor(args), wait=True
            )
        except (
            keys_svc.KeyServiceError,
            content_svc.ProjectArchivedError,
            wf_service.WorkflowArchivedError,
        ) as exc:
            # TranslationEngineError (mock-409) is a KeyServiceError subclass, so the
            # no-engine refusal is named here too -- all clean EXIT_USAGE, no traceback.
            raise CliError(str(exc), EXIT_USAGE) from exc
        except orchestrator.RunCapacityError as exc:
            # CLI twin of the /api 503 (gate A round 1, MED-1): the fan-out reserves every
            # slot up front, so a refusal means NOTHING launched. Retryable (EXIT_FAIL),
            # not a usage error — the command is fine, the install is busy.
            raise CliError(str(exc), EXIT_FAIL) from exc
        emit_translate(emit, {"project_id": project.id, **outcome})
    # F3: a synchronous drain that ended on a FAILED / still-running run is NOT a clean
    # success. emit_translate already reported what LAUNCHED; name the ones that did not
    # finish and exit non-zero so a script does not treat a failed run as done.
    incomplete = outcome.get("drain_incomplete") or []
    if incomplete:
        for item in incomplete:
            emit.warn(
                "Run #{} did not complete (status={}); resolve or re-run before "
                "relying on this translation.".format(item["run_id"], item["status"])
            )
        return EXIT_FAIL
    # GitHub #213 part 2 (fix round 1): conflicts exit non-zero ONLY when nothing was
    # started, attached or queued -- an all-conflicted fan-out launched nothing, and a
    # script must not read that as success. A partial fan-out (some locale started) is
    # an honest partial success, exit 0, with every conflict line already printed. The
    # --remote arm needs no twin here: an all-conflicted call is the API's 409, and
    # `_remote.RemoteClient._decode` already raises the verbatim sentence non-zero.
    conflicts = outcome.get("conflicts") or []
    _started_anything = (
        outcome["started"]
        or outcome["restarted"]
        or outcome["busy"]
        or outcome["paused"]
        or outcome.get("queued")
    )
    if conflicts and not _started_anything:
        return EXIT_FAIL
    return 0


def _resolve_workflow(session, args):
    """The workflow `keys translate` targets, org-scoped, or a clean 'no such
    workflow'. Mirrors cmd_workflow._get_wf so the CLI can never launch across an org
    boundary."""
    from app.models import Workflow

    return get_scoped_or_404(
        session.get(Workflow, args.workflow_id), args.workflow_id, "workflow", op_org_id(args)
    )


def emit_translate(emit, payload) -> None:
    """Shared renderer for `keys translate` (local + --remote): mirrors the /api
    response (launched / started / restarted / busy / paused / skipped / conflicts)."""
    started = payload.get("started") or []
    restarted = payload.get("restarted") or []
    busy = payload.get("busy") or []
    paused = payload.get("paused") or []
    skipped = payload.get("skipped") or []
    # GitHub #213 part 2 (fix round 1): pairs REFUSED because a run of a DIFFERENT
    # workflow is already in flight on that (document, locale). This renderer is shared
    # by the local and --remote arms, so one conflict line covers both CLI arms; the
    # row's own `reason` is the standard refusal sentence naming the running run, its
    # workflow and both cancel paths. The first build dropped the list on every surface.
    conflicts = payload.get("conflicts") or []
    # The pairs this install had no free run slot for, each already a durable QUEUED run row
    # (gate A round 3, HIGH-1). The JSON arm has always carried them (it splats the payload);
    # the HUMAN arm did not, so `loc keys translate` — local or --remote — reported a
    # five-pair fan-out as "Started 2 run(s)" and said nothing about the other three.
    queued = payload.get("waiting", payload.get("queued")) or []
    queue_incomplete = payload.get("queue_incomplete") or []
    launched = payload.get("launched")
    if launched is None:
        launched = started + restarted
    if emit.as_json:
        # ok:false when a drained run did not reach a clean terminal state (F3), or when
        # conflicts refused EVERYTHING this call could launch (fix round 1: an all-
        # conflicted fan-out started nothing, and `ok: true` would be the same lie in
        # JSON). The /api-sourced payload (--remote) never carries drain_incomplete, so
        # ok stays True there -- the remote arm polls the run to a verdict itself, and an
        # all-conflicted --remote never reaches this renderer (the 409 raises first).
        _nothing_ran = not (launched or busy or paused or queued)
        _ok = not payload.get("drain_incomplete") and not (conflicts and _nothing_ran)
        emit.json({"ok": _ok, **payload})
        return

    def _say_conflicts() -> None:
        # The row's OWN reason sentence (the RunConflictError text) -- one sentence on
        # every surface, paraphrased nowhere.
        for row in conflicts:
            _loc, _dn = row.get("locale"), row.get("document_name")
            _reason = row.get("reason") or ""
            emit.say(f"{_loc} ({_dn}): {_reason}" if _dn else f"{_loc}: {_reason}")

    if not launched and not busy and not paused and not queued and not queue_incomplete:
        # The "add keys first" line is never shown for a CONFLICTED locale (fix round 1):
        # the keys are there, another workflow's run is what stopped the launch.
        _say_conflicts()
        if skipped or not conflicts:
            emit.say(
                "Nothing to translate%s."
                % (" into " + ", ".join(str(s) for s in skipped) if skipped else "")
            )
        return
    _ids = lambda rs: ", ".join("#%d" % r for r in rs)  # noqa: E731, UP031
    if started:
        emit.say("Started %d run(s): %s" % (len(started), _ids(started)))  # noqa: UP031
    if restarted:
        emit.say("Restarted %d stale run(s): %s" % (len(restarted), _ids(restarted)))  # noqa: UP031
    if busy:
        emit.say("%d locale(s) already running: %s" % (len(busy), _ids(busy)))  # noqa: UP031
    if paused:
        emit.say("%d run(s) paused for review: %s" % (len(paused), _ids(paused)))  # noqa: UP031
    if queued:
        _qids = ", ".join("#%d" % r["run_id"] for r in queued if r.get("run_id"))  # noqa: UP031
        emit.say(
            "Queued %d run(s) waiting for a free slot%s: they start automatically as the "  # noqa: UP031
            "running work finishes (cancel one with `workflow cancel-run`)."
            % (len(queued), (": " + _qids) if _qids else "")
        )
    for _row in queue_incomplete:
        # A pair whose run row could not be WRITTEN. Never silent (gate A round 3, HIGH-2).
        emit.warn(
            "{}: {} could not be queued ({}). Retry it.".format(
                _row.get("locale"), _row.get("document_name") or "", _row.get("reason") or ""
            )
        )
    if skipped:
        emit.say("Nothing to translate into {}.".format(", ".join(str(s) for s in skipped)))
    # #213 part 2 (fix round 1): a PARTIAL fan-out still prints every conflict line,
    # next to what started -- the operator must see which locale is bound to another
    # workflow's running run and how to release it.
    _say_conflicts()


# --- per-string in-context images (Batch 14 T1) ------------------------------
def _image_flag_gate() -> None:
    from app.kernel import key_images

    if not key_images.enabled():
        raise CliError(
            "image translation context is disabled (setting key_image_context_enabled=0)."
        )


def _image_key(session, args):
    """Resolve the key by id, org-scoped, and confine it to a key-based project. A missing
    / cross-org / soft-deleted key, or one on a document project, gets the SAME clean
    refusal -- the two project types never leak each other's shape (the `_project` idiom)."""
    from app.kernel import keys as keys_svc
    from app.models import Project, StringKey

    key = session.get(StringKey, args.key_id)
    if key is None or key.deleted_at is not None:
        raise CliError(f"no such key: #{int(args.key_id)}", EXIT_USAGE)
    project = get_scoped_or_404(
        session.get(Project, key.project_id), key.project_id, "project", op_org_id(args)
    )
    if not keys_svc.is_key_project(project):
        raise CliError(f"key #{int(args.key_id)} is not on a key-based project.", EXIT_USAGE)
    return project, key


def _keys_image_add(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_image_add(args, emit)
    require_org_scope(args, emit)
    _flag_gate()  # the parent `keys` module must be on too
    _image_flag_gate()
    from pathlib import Path

    from app import config
    from app.kernel import key_images

    src = Path(str(args.path)).expanduser()
    if not src.is_file():
        raise CliError(f"no such file: {src}", EXIT_USAGE)
    data = src.read_bytes()
    if len(data) > config.KEY_IMAGE_MAX_BYTES:
        raise CliError("that image is too large.", EXIT_USAGE)
    with _session() as session:
        _project, key = _image_key(session, args)
        try:
            img = key_images.attach_image(
                session, key, data, caption=args.caption or "", uploaded_by=_actor(args)
            )
        except key_images.KeyImageError as exc:
            session.rollback()
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Attached image #{img.id} to key #{key.id}.",
            {"ok": True, "image": key_images.view(img)},
        )
    return 0


def image_lines(views) -> list[str]:
    """The human list both the local and --remote arm print, byte-identically."""
    return [
        "#{} {}{}{} {}x{} {}".format(
            v.get("id"),
            "[primary] " if v.get("is_primary") else "",
            "" if v.get("ready", True) else "[pending] ",
            (v.get("caption") or "(no caption)"),
            v.get("width"),
            v.get("height"),
            v.get("mime"),
        )
        for v in views
    ]


def _keys_image_list(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_image_list(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    _image_flag_gate()
    from app.kernel import key_images

    with _session() as session:
        _project, key = _image_key(session, args)
        views = [key_images.view(i) for i in key_images.list_images(session, key)]
        human = (
            f"Key #{key.id} has {len(views)} image(s):\n" + "\n".join(image_lines(views))
            if views
            else f"Key #{key.id} has no images."
        )
        emit.result(human, {"ok": True, "key_id": key.id, "images": views})
    return 0


def _resolve_image(session, key, image_id, *, include_pending=False):
    from app.kernel import key_images

    img = key_images.get_image(session, key, image_id, include_pending=include_pending)
    if img is None:
        raise CliError(f"no such image #{int(image_id)} on key #{key.id}.", EXIT_USAGE)
    return img


def _keys_image_rm(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_image_rm(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    _image_flag_gate()
    from app.kernel import key_images

    with _session() as session:
        _project, key = _image_key(session, args)
        img = _resolve_image(session, key, args.image_id, include_pending=True)
        key_images.delete_image(session, key, img, actor=_actor(args))
        emit.result(
            f"Deleted image #{args.image_id} from key #{key.id}.",
            {"ok": True, "id": args.image_id, "deleted": True},
        )
    return 0


def _keys_image_set_primary(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_image_set_primary(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    _image_flag_gate()
    from app.kernel import key_images

    with _session() as session:
        _project, key = _image_key(session, args)
        img = _resolve_image(session, key, args.image_id)
        key_images.set_primary(session, key, img, actor=_actor(args))
        emit.result(
            f"Image #{args.image_id} is now the primary for key #{key.id}.",
            {"ok": True, "image": key_images.view(img)},
        )
    return 0


__all__ = [
    "DEFAULT_CURSOR_FILE",
    "_actor",
    "_changes_mode_guard",
    "_clip",
    "_export_payload",
    "_flag_gate",
    "_image_flag_gate",
    "_image_key",
    "_keys_add",
    "_keys_changes",
    "_keys_delete",
    "_keys_deliver_approve",
    "_keys_deliver_revoke",
    "_keys_edit",
    "_keys_empties",
    "_keys_export",
    "_keys_image_add",
    "_keys_image_list",
    "_keys_image_rm",
    "_keys_image_set_primary",
    "_keys_import",
    "_keys_list",
    "_keys_restore",
    "_keys_set",
    "_keys_translate",
    "_label",
    "_plural_base_hint",
    "_project",
    "_project_arg",
    "_resolve_image",
    "_resolve_key",
    "_resolve_workflow",
    "edit_fields",
    "emit_changes",
    "emit_export",
    "emit_import_report",
    "emit_key_list",
    "emit_translate",
    "image_lines",
    "key_rows",
    "locales_of",
    "persist_cursor",
    "read_cursor_file",
    "register",
    "register_changes",
    "register_import_export",
    "resolve_cursor",
    "write_cursor_file",
]
