"""File transfer and change-feed CLI operations for key-based projects.

This module owns import/export, empty-value delivery gates, and cursor-based
sync, including parser registration and output for those operations.
`keys changes --cursor-file` persists `next_cursor` in a small JSON file so CI
consumers can resume without a server-side subscription.

Every verb dispatches remotely or refuses loudly when `--remote` is requested.
A past local-session fall-through misdirected a production upload;
`verify_cli_remote_dispatch_guard` scans the parser tree to prevent recurrence.

Documented in: docs/architecture/string-keys.md
"""

import json
import os

from app.cli.keys_common import (
    _actor,
    _clip,
    _flag_gate,
    _label,
    _project,
    _project_arg,
)

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    confirm,
    remote_mode,
    require_local,
    require_org_scope,
)

#: Where `keys changes --cursor-file` parks the cursor when no path is given.
DEFAULT_CURSOR_FILE = ".locstudio-sync.json"


def register_import_export(sub) -> None:
    p = sub.add_parser(
        "import",
        help="Import a key file: JSON, gettext .po, or a resource file (.properties, "
        ".strings, YAML, ...).",
    )
    _project_arg(p)
    p.add_argument("--file", required=True, help="path to the key file.")
    p.add_argument(
        "--locale",
        default="",
        help="the locale this file carries (default: the project's "
        "source locale, i.e. base values).",
    )
    p.add_argument(
        "--format",
        default="",
        dest="fmt",
        help="override the format inferred from the file name: json, po, or any resource "
        "format with a key view (properties, strings, ini, resjson, yaml, ...). An unknown "
        "value is refused with the full list.",
    )
    p.add_argument(
        "--namespace",
        default="",
        help="namespace for every key (default: the file stem). Use "
        "'@from-key' to read it off the front of each name, which "
        "is how a whole-project export is re-imported.",
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="DESTRUCTIVE: soft-delete every key in the namespace the "
        "file does not mention. Base-locale imports only.",
    )
    p.add_argument("--yes", action="store_true", help="skip the --cleanup confirmation.")
    p.set_defaults(func=_keys_import)

    p = sub.add_parser("export", help="Export one locale as a key file.")
    _project_arg(p)
    p.add_argument(
        "--locale",
        default="",
        help="the locale to export (default: the source locale, which "
        "emits the keys' base values).",
    )
    p.add_argument(
        "--format",
        default="json",
        dest="fmt",
        help="output format (default: json): json, po, or a resource format imported "
        "into --namespace (properties, strings, yaml, ...), which fills that "
        "namespace's source file.",
    )
    p.add_argument(
        "--namespace",
        default="",
        help="export ONE namespace as a BUILD file (bare key names). "
        "Omit for the whole project: a TRANSPORT file with every "
        "name qualified 'namespace::name' and a marker that says so.",
    )
    p.add_argument(
        "--empty-as",
        default="empty",
        dest="empty_as",
        choices=["base", "empty", "skip"],
        help="what to write for an untranslated key (default: empty).",
    )
    p.add_argument(
        "--status",
        default="",
        help="only export keys with a value in "
        "this segment status (new, mt, edited, needs_review, approved). "
        "KEY-LEVEL: a key ships WHOLE if any of its per-locale values "
        "carries the status, else it is omitted whole. No effect on a "
        "source-locale (base-value) export, which has no per-locale status.",
    )
    p.add_argument(
        "--archived", action="store_true", help="include archived keys (excluded by default)."
    )
    p.add_argument(
        "--include-string-images",
        action="store_true",
        dest="include_string_images",
        help="bundle each exported key's in-context images: the output becomes a ZIP "
        "(key file + images/ + manifest.json linking key to image path). Requires -o "
        "(the bundle is binary). Ignored when the image-context feature is off.",
    )
    p.add_argument("-o", "--out", help="destination path (default: stdout).")
    p.add_argument("--force", action="store_true", help="overwrite --out if it exists.")
    p.set_defaults(func=_keys_export)

    # --- The delivery empty gate (Batch 15 T2, key/project mode) -------------
    # #105 round-1 MED-2: names the CONDITION, not "a TM-only workflow" (the gate keys on the
    # durable tm_match marker, so a tm_match -> llm_agent run is held identically).
    p = sub.add_parser(
        "empties",
        help="List a key project's untranslated (empty) keys for a locale that the "
        "delivery empty gate is holding back from delivery.",
    )
    _project_arg(p)
    p.add_argument("--locale", default="", help="the locale (default: the source locale).")
    p.set_defaults(func=_keys_empties)

    p = sub.add_parser(
        "deliver-approve",
        help="Approve delivering a held key project's empty values for a locale "
        "(audited; the export renders them per --empty-as). Refused while a run "
        "is still in flight over the project's key documents.",
    )
    _project_arg(p)
    p.add_argument("--locale", default="", help="the locale (default: the source locale).")
    p.set_defaults(func=_keys_deliver_approve)

    p = sub.add_parser(
        "deliver-revoke",
        help="Revoke a prior empty-delivery approval for a key project + locale -> re-held.",
    )
    _project_arg(p)
    p.add_argument("--locale", default="", help="the locale (default: the source locale).")
    p.set_defaults(func=_keys_deliver_revoke)


def register_changes(sub) -> None:
    p = sub.add_parser(
        "changes",
        help="Read the change feed since a cursor "
        "(deletions included as first-class entries), or the "
        "current-state feed of values changed after a timestamp.",
    )
    _project_arg(p)
    p.add_argument(
        "--since",
        type=int,
        default=None,
        help="CHANGELOG mode: cursor to read after (default: 0, a full "
        "bootstrap, or the value stored in --cursor-file). Reports "
        "every op incl. deletions. An unparseable value is refused.",
    )
    p.add_argument(
        "--cursor-file",
        nargs="?",
        const=DEFAULT_CURSOR_FILE,
        default=None,
        dest="cursor_file",
        help="read the starting cursor from this file and write the new "
        f"next_cursor back to it (default path: {DEFAULT_CURSOR_FILE}). Changelog mode "
        "only (mutually exclusive with --modified-after).",
    )
    p.add_argument(
        "--modified-after",
        default=None,
        dest="modified_after",
        metavar="ISO",
        help="MODIFIED-AFTER mode: return current-state value rows of keys "
        "whose content or status changed after this ISO-8601 UTC "
        "timestamp (source edits count too; a naive value is assumed "
        "UTC). NO deletions -- use --since for those. Keyset-paged: "
        "feed next_modified_after + next_after_id back. Mutually "
        "exclusive with --since/--cursor-file.",
    )
    p.add_argument(
        "--after-id",
        type=int,
        default=None,
        dest="after_id",
        help="modified-after mode: the keyset cursor's id part (pass the "
        "next_after_id from the previous page alongside --modified-after "
        "= next_modified_after).",
    )
    p.add_argument(
        "--locale",
        default=None,
        help="modified-after mode ONLY: narrow to one target locale "
        "(validated against the project's locales).",
    )
    p.add_argument(
        "--limit", type=int, default=0, help="maximum keys per page (server default 500, cap 1000)."
    )
    p.set_defaults(func=_keys_changes)


def emit_changes(emit, payload) -> None:
    """The shared renderer for `keys changes` (local + --remote), both modes."""
    if emit.as_json:
        emit.json({"ok": True, **payload})
        return
    if payload.get("resync_required"):
        emit.warn(
            payload.get("message")
            or "cursor is older than the retained history; re-bootstrap with --since 0."
        )
        return
    if payload.get("mode") == "modified_after":
        rows = [
            (
                e.get("target_locale") or "-",
                e.get("plural_category") or "-",
                _label(e["namespace"], e["name"]),
                _clip(e.get("text") or "", 24),
                (e.get("content_hash") or "")[:12],
            )
            for e in payload.get("entries", [])
        ]
        emit.table(rows, headers=("locale", "form", "key", "text", "hash"))
        # next_modified_after is the self-contained `ISO~id` token while paging, so
        # feeding it straight back as --modified-after resumes atomically (NEW-3).
        emit.say(
            "next: --modified-after {}{}".format(
                payload.get("next_modified_after"),
                "  (more available)" if payload.get("has_more") else "",
            )
        )
        return
    rows = [
        (
            e["cursor"],
            e["entity"],
            e["op"],
            _label(e["namespace"], e["name"]),
            e.get("target_locale") or "-",
            e.get("plural_category") or "-",
            _clip(e.get("actor") or "-", 24),
        )
        for e in payload.get("entries", [])
    ]
    emit.table(rows, headers=("cursor", "entity", "op", "key", "locale", "form", "actor"))
    emit.say(
        "next_cursor: {}{}".format(
            payload.get("next_cursor"), "  (more available)" if payload.get("has_more") else ""
        )
    )


def read_cursor_file(path: str, project_id=None, emit=None) -> int:
    """The stored cursor, or 0. A missing/unreadable/garbage file reads as 0 (a full
    bootstrap) rather than an error: a first run has no file, and re-bootstrapping is
    always SAFE -- entries are idempotent by key identity. The dangerous direction is
    a cursor that is too HIGH, which is why nothing here ever guesses one.

    A CURSOR FROM ANOTHER PROJECT IS EXACTLY THAT DANGEROUS DIRECTION (review B1), and
    it is the DEFAULT shape of the CI story this flag exists for. Changelog ids are a
    GLOBAL sequence filtered per project, and the default cursor path is
    `.locstudio-sync.json` in the CURRENT DIRECTORY -- so a repo that syncs two key
    projects had project B read project A's high-water mark, ask for everything after
    it, and receive NOTHING. `resync_required` was false (the cursor is above the
    floor, not below it), the file was rewritten with the same number, and B never
    caught up. Silent, permanent, and indistinguishable from "nothing changed" -- the
    one answer this feed promises never to give wrongly.

    So a stored `project_id` that does not match the requested one reads as 0: a full
    bootstrap, which is always safe, with a note on stderr saying why."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        cursor = max(0, int(data.get("next_cursor") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    stored = data.get("project_id") if isinstance(data, dict) else None
    if project_id is not None and stored is not None and str(stored) != str(project_id):
        if emit is not None:
            emit.warn(
                f"{path} holds a cursor for project {stored}, not {project_id} -- bootstrapping "
                "from 0 instead. Use a per-project --cursor-file path."
            )
        return 0
    return cursor


def write_cursor_file(path: str, project_id, cursor) -> None:
    """Persist `next_cursor` for the next poll. Written only AFTER the entries have
    been printed, so a consumer that dies mid-render re-reads the same window instead
    of skipping it."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"project_id": project_id, "next_cursor": int(cursor)}, fh)
        fh.write("\n")


def _keys_import(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_import(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import key_files
    from app.kernel import keys as keys_svc

    if not os.path.isfile(args.file):
        raise CliError(f"file not found: {args.file}", EXIT_USAGE)
    with open(args.file, "rb") as fh:
        data = fh.read()
    if args.cleanup and not confirm(
        emit,
        "Cleanup soft-DELETES every key in this namespace the file does not mention. Continue?",
        args.yes,
    ):
        raise CliError("aborted.", EXIT_USAGE)
    ns = (args.namespace or "").strip()
    with _session() as session:
        project = _project(session, args)
        from app.kernel import connected_projects

        if connected_projects.is_connected(project):
            raise CliError(connected_projects.UPLOAD_REFUSED, EXIT_USAGE)
        try:
            report = key_files.import_keys(
                session,
                project,
                os.path.basename(args.file),
                data,
                fmt=args.fmt,
                locale=args.locale,
                mode=(key_files.MODE_CLEANUP if args.cleanup else key_files.MODE_MERGE),
                namespace=(key_files.NS_FROM_KEY if ns == "@from-key" else (ns if ns else None)),
                actor=_actor(args),
            )
        except keys_svc.KeyServiceError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit_import_report(emit, report)
    return 0


def emit_import_report(emit, report) -> None:
    """The shared import renderer (local + --remote). ERRORS AND NOTES ARE PRINTED,
    never swallowed: partial success is the contract, so an operator has to be able
    to see which keys did not land and why."""
    if emit.as_json:
        emit.json({"ok": True, **report})
        return
    emit.say(
        "Imported %s (%s, %s, %s): %d created, %d updated, %d unchanged, "  # noqa: UP031
        "%d deleted, %d error(s)."
        % (
            report.get("filename"),
            report.get("fmt"),
            report.get("locale"),
            report.get("mode"),
            report.get("created", 0),
            report.get("updated", 0),
            report.get("unchanged", 0),
            report.get("deleted", 0),
            len(report.get("errors") or []),
        )
    )
    for err in report.get("errors") or []:
        emit.warn("  {}: {}".format(err.get("name") or err.get("key") or "?", err.get("error")))
    for note in report.get("notes") or []:
        emit.say(f"  note: {note}")


def _keys_export(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.keys_export(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import export, key_files, key_images
    from app.kernel import keys as keys_svc

    # L8 parity: the `-o` guard fires on the FLAG itself, BEFORE the feature-enabled check, so
    # `--include-string-images` without `-o` is refused identically whether or not the image
    # feature is on (the remote arm cannot see the local flag either and refuses the same way).
    # Only then does `want_images` fold in the feature gate (feature off + `-o` -> a plain file).
    flag_set = bool(getattr(args, "include_string_images", False))
    if flag_set and not getattr(args, "out", None):
        # A bundle is a binary ZIP: refuse the stdout path rather than corrupt it through the
        # utf-8 decode in emit_export (a redirect would still land, but silently mangled).
        raise CliError(
            "--include-string-images produces a binary ZIP bundle; pass -o/--out to write it "
            "to a file (stdout is text-only).",
            EXIT_USAGE,
        )
    want_images = flag_set and key_images.enabled()
    rep: list = []
    ekeys: list = [] if want_images else None
    bundle = None
    img_info: dict = {}
    with _session() as session:
        project = _project(session, args)
        try:
            data = key_files.export_locale(
                session,
                project,
                args.locale,
                fmt=args.fmt,
                namespace=(args.namespace if (args.namespace or "").strip() else None),
                empty_as=args.empty_as,
                include_archived=bool(args.archived),
                status=((args.status or "").strip() or None),
                report=rep,
                exported_keys=ekeys,
            )
            if want_images:
                from datetime import datetime, timezone

                loc = (args.locale or "").strip() or project.source_locale
                stem = (args.namespace or "").strip() or "project"
                fname = key_files.download_filename(stem, loc, key_files.export_ext(args.fmt))
                try:
                    bundle, img_info = key_images.build_export_bundle(
                        session,
                        project,
                        loc,
                        key_file_bytes=data,
                        key_file_name=fname,
                        exported_keys=ekeys,
                        qualify=(not (args.namespace or "").strip()),
                        generated_at=datetime.now(timezone.utc),
                    )
                except key_images.KeyImageError as exc:
                    # L1: carry the STRUCTURED code (key_image_export_max on a cap refusal) on the
                    # --json error object too, so the LOCAL and --remote arms emit the same error
                    # shape -- not just the same success shape.
                    raise CliError(
                        str(exc),
                        EXIT_USAGE,
                        payload=({"code": exc.code} if getattr(exc, "code", None) else None),
                    ) from exc
        except export.ExportEmptyTargetsHeld as exc:
            # Batch 15 T2 (§9.2): the TM-only empty gate held this key export. The way out
            # is the AUDITED approval (keys deliver-approve), never a source-filled file.
            raise CliError(
                f"export refused: {exc} Run `loc-tms keys deliver-approve "
                f"--project {project.id} --locale {exc.locale}` to approve delivering "
                "empty values.",
                EXIT_USAGE,
            ) from exc
        except keys_svc.KeyServiceError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    if bundle is not None:
        # The bundle is a spooled ZIP: STREAM it to --out (guaranteed present by the guard
        # above) in chunks so it is never re-loaded whole into memory (M2), then report the
        # bundle facts on --json (L10: image count + bytes, which web/api put in the audit row).
        out = args.out
        if os.path.exists(out) and not args.force:
            bundle.close()
            raise CliError(f"destination exists: {out} (pass --force to overwrite).", EXIT_USAGE)
        total = 0
        with open(out, "wb") as fh:
            for block in key_images.iter_bundle(bundle):
                fh.write(block)
                total += len(block)
        extra = _export_payload(rep)
        notices = extra.pop("notices")
        emit.result(
            "Exported %d byte(s) -> %s" % (total, out),  # noqa: UP031
            {
                "ok": True,
                "bytes": total,
                "path": out,
                "img_info": {
                    "image_count": img_info.get("image_count", 0),
                    "image_bytes": img_info.get("image_bytes", 0),
                },
                **extra,
            },
        )
        for line in notices:
            emit.warn(line)
        return 0
    emit_export(emit, args, data, report=rep)
    return 0


# --- TM-only delivery empty gate (Batch 15 T2, key/project mode) ------------
def _keys_empties(args, emit) -> int:
    require_local(args)  # local-only: refuses --remote loudly (no silent local read)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import tm_delivery

    with _session() as session:
        project = _project(session, args)
        loc = (args.locale or "").strip() or project.source_locale
        # M1: review surface -> widest universe (matches the key_empties list + approve cover).
        decision = tm_delivery.empty_gate_decision(
            session, project=project, locale=loc, include_archived=True, include_hidden=True
        )
        empties = tm_delivery.key_empties(session, project, loc)
        row = tm_delivery._key_approval_row(session, project.id, loc)
        approved = bool(row is not None and row.status == "approved")
        for e in empties:
            emit.say(f"  {e['name']}: {e['source_text']}")
        emit.result(
            f"Project #{project.id} ({loc}): {len(empties)} empty key(s); "
            f"decision={decision}; approved={approved}.",
            {
                "ok": True,
                "project_id": project.id,
                "locale": loc,
                "decision": decision,
                "empty_count": len(empties),
                "empties": empties,
                "approved": approved,
            },
        )
    return 0


def _keys_deliver_approve(args, emit) -> int:
    require_local(args)  # local-only: refuses --remote loudly
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import tm_delivery

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _project(session, args)
        try:
            row = tm_delivery.approve_keys(session, project, args.locale, approved_by=actor)
        except tm_delivery.LocaleNotDeliverable as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Approved empty delivery for project #{project.id} ({row.locale}); "
            f"covered {row.covered_count} empty key(s).",
            {
                "ok": True,
                "project_id": project.id,
                "locale": row.locale,
                "approved": True,
                "covered_count": row.covered_count,
            },
        )
    return 0


def _keys_deliver_revoke(args, emit) -> int:
    require_local(args)  # local-only: refuses --remote loudly
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import tm_delivery

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        project = _project(session, args)
        loc = (args.locale or "").strip() or project.source_locale
        try:
            revoked = tm_delivery.revoke_keys(session, project, loc, approved_by=actor)
        except tm_delivery.LocaleNotDeliverable as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            (
                f"Revoked empty-delivery approval for project #{project.id} ({loc})."
                if revoked
                else f"No empty-delivery approval to revoke for project #{project.id} "
                f"({loc}) (already held)."
            ),
            {"ok": True, "project_id": project.id, "locale": loc, "revoked": revoked},
        )
    return 0


def _export_payload(report) -> dict:
    """The tag-guard facts every export payload carries (live-QA K2).

    The payload used to be `{ok, bytes, path}` and nothing else, so a build file in
    which five German strings had silently been replaced by ENGLISH was indistinguishable
    from a clean one -- on the one surface (`--json`) a release pipeline actually reads."""
    from app.kernel import key_files

    subs = key_files.substituted_keys(report)
    kept = key_files.kept_damaged_keys(report)
    withheld = key_files.empty_as_base_keys(report)
    over = key_files.over_char_limit_keys(report)
    filtered = key_files.filtered_out_keys(report)
    missing = key_files.not_in_template_keys(report)
    return {
        "substituted": len(subs),
        "substituted_keys": subs,
        "kept_damaged": len(kept),
        "kept_damaged_keys": kept,
        # `--empty-as base` filling a WITHHELD (empty + needs-review) value with
        # its source text (fix-round K12) -- the export answering a question the
        # product had refused to answer, so a pipeline gets to see it.
        "withheld_filled": len(withheld),
        "withheld_filled_keys": withheld,
        # K8 (spec section 7): values that SHIPPED but exceed their character limit -- a
        # distinct signal from a status filter, so a build pipeline can act on it.
        "over_char_limit": len(over),
        "over_char_limit_keys": over,
        # K9 (live-QA F3): keys the `--status` filter DROPPED whole. The one export
        # outcome that used to be silent -- so a bogus filter's valid-but-empty file
        # is now distinguishable from a genuinely empty project.
        "filtered_out": len(filtered),
        "filtered_out_keys": filtered,
        # Batch 9: keys a resource-file export had no place for in its template.
        "not_in_template": len(missing),
        "not_in_template_keys": missing,
        "notices": key_files.export_notices(report),
    }


def emit_export(emit, args, data: bytes, report=None) -> None:
    """Write (or print) the exported bytes. Shared so --remote lands byte-identically.

    `report` is `export_locale`'s substitution channel (the `--remote` arm rebuilds it
    from the response headers). Its facts ride on BOTH surfaces: the `--json` payload
    and, as warnings on stderr, the human one -- stderr so a `keys export > file.json`
    redirect still gets clean bytes."""
    extra = _export_payload(report)
    notices = extra.pop("notices")
    if getattr(args, "out", None):
        if os.path.exists(args.out) and not args.force:
            raise CliError(
                f"destination exists: {args.out} (pass --force to overwrite).",
                EXIT_USAGE,  # noqa: RUF100, UP032
            )
        with open(args.out, "wb") as fh:
            fh.write(data)
        emit.result(
            "Exported %d byte(s) -> %s" % (len(data), args.out),  # noqa: UP031
            {"ok": True, "bytes": len(data), "path": args.out, **extra},
        )
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # A resource file keeps its source encoding (UTF-16 .strings, Latin-1
            # .properties); printing it would corrupt it. Ask for a file instead.
            raise CliError(
                "this export is not UTF-8 text (it keeps the source file's encoding); "
                "pass -o/--out to write it to a file.",
                EXIT_USAGE,
            ) from None
        if emit.as_json:
            emit.json({"ok": True, "bytes": len(data), "content": text, **extra})
        else:
            emit.say(text)
    for line in notices:
        emit.warn(line)


def _changes_mode_guard(args) -> None:
    """The client-input checks that hold on BOTH the local and --remote arm (gate
    H2a/M6): the two modes are mutually exclusive, and `--locale`/`--after-id` belong
    ONLY to modified_after mode. Raised as EXIT_USAGE (exit 2) BEFORE any DB/HTTP work,
    so a bad combination is refused identically and without a round-trip."""
    modified_after = getattr(args, "modified_after", None)
    if modified_after is not None and (args.since is not None or args.cursor_file):
        raise CliError(
            "--modified-after is mutually exclusive with --since/--cursor-file (pass one).",
            EXIT_USAGE,
        )
    if modified_after is None and (
        getattr(args, "locale", None) is not None or getattr(args, "after_id", None) is not None
    ):
        raise CliError("--locale/--after-id are only valid with --modified-after.", EXIT_USAGE)


def _keys_changes(args, emit) -> int:
    _changes_mode_guard(args)
    if remote_mode(args):
        return _remote_impl.keys_changes(args, emit)
    require_org_scope(args, emit)
    _flag_gate()
    from app.kernel import keys as keys_svc

    modified_after = getattr(args, "modified_after", None)
    limit = args.limit or keys_svc.DEFAULT_DELTA_LIMIT
    with _session() as session:
        project = _project(session, args)
        pid = project.id
        if modified_after is not None:
            try:
                payload = keys_svc.changes_modified_after(
                    session,
                    pid,
                    modified_after,
                    limit,
                    after_id=getattr(args, "after_id", None),
                    locale=(getattr(args, "locale", None) or None),
                )
            except (keys_svc.BadTimestampError, keys_svc.UnknownLocaleError) as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            emit_changes(emit, payload)
            return 0
        since, path = resolve_cursor(args, emit)
        payload = keys_svc.changes_since(session, pid, since, limit)
    emit_changes(emit, payload)
    persist_cursor(path, pid, payload)
    return 0


def resolve_cursor(args, emit=None):
    """(since, cursor_file_path). `--since` WINS over the file when both are given:
    an explicit cursor on the command line is an operator overriding their own sync
    state (re-reading a window, or forcing a bootstrap), and silently ignoring it in
    favour of a file would make that impossible.

    The project id goes down to `read_cursor_file` so a file belonging to a DIFFERENT
    project bootstraps instead of silently swallowing this one's feed (review B1)."""
    path = getattr(args, "cursor_file", None)
    if args.since is not None:
        return max(0, args.since), path
    if path:
        return read_cursor_file(path, getattr(args, "project_id", None), emit), path
    return 0, None


def persist_cursor(path, project_id, payload) -> None:
    """Store next_cursor -- but NEVER after a resync signal: the server just said the
    cursor is unusable, so writing the echoed value back would pin the consumer to a
    position it can never advance from."""
    if not path or payload.get("resync_required"):
        return
    write_cursor_file(path, project_id, payload.get("next_cursor") or 0)
