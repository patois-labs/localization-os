"""_remote_impl: part 5 (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import os

from app.cli._common import (
    EXIT_USAGE,
    CliError,
)
from app.cli._remote_impl_common import (
    _client,
    _keys_lookup,
    _read_bytes,
)


def _keys_find(c, args, include_deleted=False):
    """`_keys_lookup`, but a miss is the SAME refusal the local arm gives."""
    from .cmd_keys import _label

    key, source_locale = _keys_lookup(c, args, include_deleted=include_deleted)
    if key is None:
        raise CliError(
            "no such key: {}".format(
                _label(
                    getattr(args, "namespace", "") or "",
                    args.name,
                    getattr(args, "context", "") or "",
                )
            ),
            EXIT_USAGE,
        )
    return key, source_locale


def keys_add(args, emit) -> int:
    from .cmd_keys import _label

    c = _client(args)
    # PARITY WITH THE LOCAL ARM (review B2). `POST /api/projects/{id}/keys` is the
    # BULK UPSERT route -- a CI job pushing 4,000 keys must not lose 3,999 to one
    # collision -- so dispatching a single `keys add` at it turned the local arm's
    # loud DuplicateKeyError into a SILENT overwrite of the existing base value. Two
    # surfaces of the same verb answered the same input with "refused" and "your
    # source string is gone". The pre-check restores the refusal (message and exit
    # code both from the service's own exception, so they cannot drift); a genuine
    # bulk upsert is what `keys import` is for.
    from app.kernel.keys import KEY_NAME_UNAVAILABLE, DuplicateKeyError

    def _taken():
        return CliError(
            str(DuplicateKeyError(args.namespace or "", args.name, args.context or "")), EXIT_USAGE
        )

    existing, _src = _keys_lookup(c, args)
    if existing is not None:
        raise _taken()
    base = args.base if (args.base or "").strip() else None
    rec = {
        "name": args.name,
        "namespace": args.namespace,
        "context": args.context,
        "description": args.description,
        "is_plural": bool(args.plural),
    }
    if args.tags:
        rec["tags"] = list(args.tags)
    if args.char_limit is not None:
        rec["char_limit"] = args.char_limit
    if base is not None:
        rec["base"] = {"other": base} if args.plural else base
    body = c.post(f"/api/projects/{args.project_id}/keys", json={"keys": [rec]})
    errors = body.get("errors") or []
    if errors:
        if errors[0].get("error") == KEY_NAME_UNAVAILABLE:
            # #574: a key hidden from this caller is invisible to the lookup above and the
            # upsert refuses it per row; answer exactly as for a visible taken name (same
            # sentence, same exit code) so the two cannot be told apart.
            raise _taken()
        raise CliError(str(errors[0].get("error") or "the key was refused"))
    new_ids = body.get("created") or body.get("updated") or []
    key_id = new_ids[0] if new_ids else None
    view = c.get(f"/api/keys/{key_id}/values")["key"] if key_id else {}
    emit.result(
        f"Added key #{key_id}: {_label(args.namespace, args.name, args.context)}",
        {"ok": True, "key": view},
    )
    return 0


def keys_list(args, emit) -> int:
    from .cmd_keys import emit_key_list

    c = _client(args)
    params = {
        "cursor": args.cursor,
        "limit": args.limit,
        "archived": 1 if args.archived else 0,
        "include_deleted": 1 if getattr(args, "include_deleted", False) else 0,
    }
    if args.namespace is not None:
        params["namespace"] = args.namespace
    for flag, name in (
        ("tag", "tag"),
        ("search", "q"),
        ("status", "status"),
        ("untranslated_in", "untranslated_in"),
    ):
        value = getattr(args, flag, "") or ""
        if value:
            params[name] = value
    body = c.get(f"/api/projects/{args.project_id}/keys", params=params)
    emit_key_list(emit, {k: v for k, v in body.items() if k != "ok"})
    return 0


def keys_import(args, emit) -> int:
    from ._common import confirm
    from .cmd_keys import emit_import_report

    c = _client(args)
    data = _read_bytes(args.file)
    if args.cleanup and not confirm(
        emit,
        "Cleanup soft-DELETES every key in this namespace the file does not mention. Continue?",
        args.yes,
    ):
        raise CliError("aborted.", EXIT_USAGE)
    body = c.post(
        f"/api/projects/{args.project_id}/keys/import",
        json={
            "filename": os.path.basename(args.file),
            "data_b64": base64.b64encode(data).decode(),
            "fmt": args.fmt,
            "locale": args.locale,
            "cleanup": bool(args.cleanup),
            "namespace": (args.namespace or ""),
        },
    )
    emit_import_report(emit, {k: v for k, v in body.items() if k != "ok"})
    return 0


def keys_export(args, emit) -> int:
    from app.kernel import key_files

    from ._common import EXIT_USAGE, CliError
    from .cmd_keys import _export_payload, emit_export

    want_images = bool(getattr(args, "include_string_images", False))
    if want_images and not getattr(args, "out", None):
        # The bundle is a binary ZIP: refuse the stdout path rather than corrupt it (parity
        # with the local arm's guard, which fires on the FLAG regardless of the feature gate).
        # The server still enforces the caps + feature gate.
        raise CliError(
            "--include-string-images produces a binary ZIP bundle; pass -o/--out to write it "
            "to a file (stdout is text-only).",
            EXIT_USAGE,
        )
    c = _client(args)
    params = {
        "locale": args.locale,
        "fmt": args.fmt,
        "empty_as": args.empty_as,
        "archived": 1 if args.archived else 0,
    }
    if (args.namespace or "").strip():
        params["namespace"] = args.namespace
    if (getattr(args, "status", "") or "").strip():
        params["status"] = args.status.strip()
    if want_images:
        params["include_string_images"] = 1

    path = f"/api/projects/{args.project_id}/keys/export"
    if want_images:
        # -o is guaranteed present by the guard above. STREAM the response to disk in chunks so
        # a large bundle is never resident in the client whole (L8); refuse an existing dest
        # FIRST so a failed download never truncates it.
        out = args.out
        if os.path.exists(out) and not args.force:
            raise CliError(f"destination exists: {out} (pass --force to overwrite).", EXIT_USAGE)
        try:
            total, rheaders = c.stream_to_file(path, out, params=params)
        except CliError as exc:
            # L4/L7 parity: the over-cap refusal is a USAGE error on the local arm (exit 2). The
            # remote download maps every 4xx to exit 1; re-class ONLY the image-cap refusal to
            # EXIT_USAGE, keyed on the STRUCTURED server `code` (not a substring of the message,
            # which a reword would silently break), so both arms exit the same on the same refusal.
            server_code = (getattr(exc, "payload", None) or {}).get("code")
            # L2: keep the message-substring as a FALLBACK belt behind the code check, for version
            # skew -- a server old enough to refuse on the cap without sending the structured code
            # still re-classes to exit 2 rather than reading as a generic exit-1 server error.
            if server_code == "key_image_export_max" or "key_image_export_max" in str(exc):
                raise CliError(str(exc), EXIT_USAGE, payload=exc.payload) from exc
            raise
        extra = _export_payload(key_files.report_from_headers(rheaders))
        notices = extra.pop("notices")
        ctype = (rheaders.get("content-type") or "").split(";", 1)[0].strip().lower()
        if ctype == "application/zip":
            # A real image bundle: report the image count/bytes the server put in the headers
            # (L10 parity with the local arm).
            def _hint(name: str) -> int:
                try:
                    return int(rheaders.get(name, 0))
                except (TypeError, ValueError):
                    return 0

            payload = {
                "ok": True,
                "bytes": total,
                "path": out,
                "img_info": {
                    "image_count": _hint("x-key-image-count"),
                    "image_bytes": _hint("x-key-image-bytes"),
                },
                **extra,
            }
        else:
            # Feature OFF: the server ignored the flag and returned the PLAIN file. Emit the SAME
            # shape the LOCAL arm emits for a plain `-o` export -- notably WITHOUT `img_info`
            # (L5 parity: local folds the feature gate into want_images and omits img_info).
            payload = {"ok": True, "bytes": total, "path": out, **extra}
        emit.result("Exported %d byte(s) -> %s" % (total, out), payload)  # noqa: UP031
        for line in notices:
            emit.warn(line)
        return 0

    # No image flag: the unchanged plain download path. The tag-guard facts travel in the
    # response headers (the body is the file), so the remote arm reports exactly what the local
    # one reports -- live-QA K2.
    resp = c.download(path, params=params)
    rheaders = getattr(resp, "headers", {}) or {}
    emit_export(emit, args, resp.content, report=key_files.report_from_headers(rheaders))
    return 0


def keys_translate(args, emit) -> int:
    from .cmd_keys import emit_translate

    c = _client(args)
    body = c.post(
        f"/api/projects/{args.project_id}/keys/translate",
        json={"workflow_id": args.workflow_id, "locale": args.locale},
    )
    emit_translate(emit, {k: v for k, v in body.items() if k != "ok"})
    return 0
