"""_remote_impl: part 2 (layered pure move)

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
    _gmod_row,
    _read_bytes,
    _sg_body_arg,
    _term_meta_from_args,
    _tm_query_params,
    _tm_row,
    _write_bytes,
)


def project_remove_locale(args, emit) -> int:
    """Remote-CLI parity for `project remove-locale`: calls the SAME
    /api/projects/{id}/remove-locale route. A confirm flag is required (--yes), sent
    as confirm_locale so a machine caller cannot skip the typed confirmation the web
    surface enforces."""
    if not getattr(args, "yes", False):
        emit.warn(
            "refusing a destructive remove-locale without --yes "
            "(a remote caller must confirm explicitly)."
        )
        return 1
    c = _client(args)
    body = c.post(
        f"/api/projects/{args.project_id}/remove-locale",
        json={"locale": args.locale, "confirm_locale": args.locale},
    )
    deleted = body.get("deleted", {})
    if body.get("stopped"):
        emit.result(
            f"Stopped {body.get('locale', args.locale)} on project #{args.project_id}: the next "
            "run no longer pulls, translates or writes it (nothing deleted).",
            {"ok": True, **body},
        )
        return 0
    emit.result(
        f"Removed {body.get('locale', args.locale)} from project "
        f"#{args.project_id} (deleted {deleted.get('segments', '?')} segment(s); "
        f"TM + glossary kept).",
        {"ok": True, **body},
    )
    return 0


def project_set_due(args, emit) -> int:
    """Remote-CLI parity for `project set-due` (adversarial-review FIX 5): calls
    the SAME /api/projects/{id}/due route the web form posts to. --clear (or an
    omitted date) sends a blank due_date, matching the in-process arm's convention."""
    c = _client(args)
    due = "" if getattr(args, "clear", False) else (getattr(args, "due", "") or "")
    body = c.post(f"/api/projects/{args.project_id}/due", json={"due_date": due})
    shown = body.get("due_date")
    emit.result(
        f"Project #{args.project_id} due date: {shown or '(cleared)'}.", {"ok": True, **body}
    )
    return 0


def project_apply_langset(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/projects/{args.project_id}/apply-langset", json={"langset_id": args.langset_id}
    )
    emit.result(
        f"Applied language set to project #{args.project_id}: added {body['added']} locale(s).",
        {"ok": True, **body},
    )
    return 0


def project_set_customer(args, emit) -> int:
    """Remote-CLI parity for `project set-customer`: calls the same
    /api/projects/{id}/set-customer route the web banner posts to (from-NULL only)."""
    c = _client(args)
    body = c.post(
        f"/api/projects/{args.project_id}/set-customer", json={"customer_id": args.customer_id}
    )
    emit.result(
        f"Project #{args.project_id} customer: {body.get('customer_id')}.", {"ok": True, **body}
    )
    return 0


def project_set_content_type(args, emit) -> int:
    """Remote-CLI parity for `project set-content-type`: calls the same
    /api/projects/{id}/content-type route the web form posts to. --clear (or an
    omitted id) sends a blank content_type_id, matching the in-process arm."""
    c = _client(args)
    ct_id = None if getattr(args, "clear", False) else getattr(args, "content_type_id", None)
    body = c.post(
        f"/api/projects/{args.project_id}/content-type",
        json={"content_type_id": "" if ct_id is None else ct_id},
    )
    emit.result(
        f"Project #{args.project_id} content type: {body.get('content_type_id') or '(cleared)'}.",
        {"ok": True, **body},
    )
    return 0


def project_set_intake(args, emit) -> int:
    """Remote-CLI parity for `project set-intake`: POST /api/projects/{id}/intake."""
    c = _client(args)
    body = c.post(f"/api/projects/{args.project_id}/intake", json={"intake": args.intake})
    emit.result(f"Project #{args.project_id} intake: {body.get('intake')}.", {"ok": True, **body})
    return 0


def project_make_connected(args, emit) -> int:
    """Remote-CLI parity for `project make-connected`: POST /api/projects/{id}/make-connected."""
    c = _client(args)
    conn = getattr(args, "connection", None)
    body = c.post(
        f"/api/projects/{args.project_id}/make-connected",
        json={"connection_id": conn} if conn is not None else {},
    )
    emit.result(
        f"Project #{args.project_id} is now a connected project on connection "
        f"#{body.get('connection_id')}. Nothing is scheduled or run.",
        {"ok": True, **body},
    )
    return 0


def project_rename(args, emit) -> int:
    """Remote-CLI parity for `project rename`: calls the same
    /api/projects/{id}/rename route the web form posts to. A blank name is a
    server-side 400 (-> CliError exit 1), same as the in-process arm's ValueError."""
    c = _client(args)
    body = c.post(f"/api/projects/{args.project_id}/rename", json={"name": args.name})
    emit.result(
        f"Renamed project #{body['id']} -> {body['name']}.",
        {"ok": True, "id": body["id"], "name": body["name"]},
    )
    return 0


def project_delete(args, emit) -> int:
    """Remote-CLI parity for `project delete`: the admin-only, typed-confirm cascade
    over POST /api/projects/{id}/delete. A dry_run FIRST fetches the itemized preview
    (and the exact project name the confirm must echo); --force is the CLI's typed
    confirmation on this arm, and the name is echoed SERVER-SIDE so a machine caller
    still cannot delete a project it never named. Reference blockers (connections /
    routing rules) refuse loudly with exit 2, mirroring the in-process arm."""
    c = _client(args)
    preview = c.post(f"/api/projects/{args.project_id}/delete", json={"dry_run": True})
    blockers = preview.get("blockers") or {}
    # Preflight the INDEPENDENT REFERRERS only (connections / routing rules), NOT
    # blockers["total"] -- total now also carries the forecast workflow_memory_suggestions
    # count (LOW-2), whose refusal is the tailored "archive instead" message the server raises
    # (api_cli surfaces it as a 400 on the real, non-dry-run delete below). Mirrors the
    # in-process arm (cmd_project.project_delete).
    if (blockers.get("connections", 0) or 0) + (blockers.get("routing_rules", 0) or 0) > 0:
        raise CliError(
            "cannot delete project %s: still referenced by %d connection(s) and %d "  # noqa: UP031
            "routing rule(s). Reassign or delete those first."
            % (args.project_id, blockers.get("connections", 0), blockers.get("routing_rules", 0)),
            EXIT_USAGE,
        )
    name = preview.get("project_name", "")
    cascade = preview.get("cascade") or {}
    emit.say(f"Deleting project #{args.project_id} ({name}) will destroy:")
    # Zero rows omitted, matching the in-process arm (cmd_project.project_delete) and the web
    # confirm dialog -- the two arms of this command must print the same thing.
    shown = [(k, v) for k, v in cascade.items() if k != "total" and v]
    for k, v in shown:
        emit.say("  {}: {}".format(k.replace("_", " "), v))
    if not shown:
        emit.say("  (nothing owned; only the project row is removed)")
    if not getattr(args, "force", False):
        raise CliError(
            "refusing to delete without --force (DESTRUCTIVE cascade). "
            "Re-run with --force to proceed.",
            EXIT_USAGE,
        )
    payload = {"confirm": name}
    if getattr(args, "privacy_erasure", False):
        payload["privacy_erasure"] = True
    body = c.post(f"/api/projects/{args.project_id}/delete", json=payload)
    emit.result(
        f"Deleted project #{args.project_id} ({name})."
        + (
            " Upload erasure is pending; automatic retry scheduled."
            if body.get("cascade", {}).get("file_pending")
            else ""
        )
        + (" " + body["warning"] if body.get("warning") else ""),
        {"ok": True, "id": args.project_id, "deleted": True, "cascade": body.get("cascade", {})},
    )
    return 0


# ============================================================================
# content-type
# ============================================================================
def content_type_create(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/content-types",
        json={
            "name": args.name,
            "description": getattr(args, "description", ""),
            "customer_id": getattr(args, "customer", None),
        },
    )
    emit.result(
        f"Created content type #{body['id']}: {body['name']}"
        + (f" (customer {body['customer_id']})" if body.get("customer_id") else " (global)"),
        {"ok": True, **body},
    )
    return 0


def content_type_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/content-types")
    types = body.get("content_types", [])
    if emit.as_json:
        emit.json({"ok": True, "content_types": types})
    else:
        emit.table(
            [
                (
                    t["id"],
                    t["name"],
                    t["customer_id"] if t.get("customer_id") else "global",
                    t.get("references", {}).get("total", 0),
                )
                for t in types
            ],
            headers=("id", "name", "scope", "refs"),
        )
    return 0


def content_type_rename(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/content-types/{args.content_type_id}/rename", json={"name": args.name})
    emit.result(f"Renamed content type #{body['id']} to {body['name']}.", {"ok": True, **body})
    return 0


def content_type_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if not confirm(emit, f"Delete content type #{args.content_type_id}?", args.yes):
        raise CliError("aborted.", 1)
    c.post(f"/api/content-types/{args.content_type_id}/delete")
    emit.result(
        f"Deleted content type #{args.content_type_id}.",
        {"ok": True, "content_type_id": args.content_type_id, "deleted": True},
    )
    return 0


def style_guide_create(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/style-guides",
        json={
            "name": args.name,
            "customer_id": getattr(args, "customer", None),
            "content_type_id": getattr(args, "content_type", None),
            "source_locale": getattr(args, "source", "") or "",
            "target_locale": getattr(args, "target", "") or "",
            "body": _sg_body_arg(args, default="") or "",
        },
    )
    emit.result(
        f"Created style guide #{body['id']}: {body['name']} "
        f"({body.get('scope')}) [{body.get('status')}]",
        {"ok": True, **body},
    )
    return 0


def style_guide_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/style-guides")
    guides = body.get("style_guides", [])
    if emit.as_json:
        emit.json({"ok": True, "style_guides": guides})
    else:
        emit.table(
            [
                (
                    g["id"],
                    g["name"],
                    g.get("status"),
                    g["customer_id"] if g.get("customer_id") else "org-global",
                    g.get("scope"),
                )
                for g in guides
            ],
            headers=("id", "name", "status", "customer", "scope"),
        )
    return 0


def style_guide_show(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/style-guides/{args.style_guide_id}")
    if emit.as_json:
        emit.json({"ok": True, **body})
    else:
        emit.result(
            f"#{body['id']} {body['name']} [{body.get('status')}] ({body.get('scope')})\n"
            f"  approved_by: {body.get('approved_by') or '-'}  "
            f"body_chars: {body.get('body_chars')}\n"
            f"  --- body ---\n{body.get('body') or '(empty)'}",
            {"ok": True, **body},
        )
    return 0


def style_guide_update(args, emit) -> int:
    c = _client(args)
    payload: dict = {}
    if getattr(args, "name", None) is not None:
        payload["name"] = args.name
    b = _sg_body_arg(args, default=None)
    if b is not None:
        payload["body"] = b
    if getattr(args, "clear_content_type", False):
        payload["clear_content_type"] = True
    elif getattr(args, "content_type", None) is not None:
        payload["content_type_id"] = args.content_type
    if getattr(args, "clear_pair", False):
        payload["clear_pair"] = True
    else:
        if getattr(args, "source", None) is not None:
            payload["source_locale"] = args.source
        if getattr(args, "target", None) is not None:
            payload["target_locale"] = args.target
    if not payload:
        raise CliError(
            "nothing to update (pass --name/--body/--content-type/"
            "--source/--target or a --clear-* flag).",
            EXIT_USAGE,
        )
    body = c.post(f"/api/style-guides/{args.style_guide_id}/update", json=payload)
    emit.result(
        f"Updated style guide #{body['id']} ({body.get('scope')}) [{body.get('status')}].",
        {"ok": True, **body},
    )
    return 0


def style_guide_set_status(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/style-guides/{args.style_guide_id}/set-status", json={"status": args.status}
    )
    emit.result(f"Style guide #{body['id']} is now {body.get('status')}.", {"ok": True, **body})
    return 0


def style_guide_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if not confirm(emit, f"Delete style guide #{args.style_guide_id}?", args.yes):
        raise CliError("aborted.", 1)
    c.post(f"/api/style-guides/{args.style_guide_id}/delete")
    emit.result(
        f"Deleted style guide #{args.style_guide_id}.",
        {"ok": True, "style_guide_id": args.style_guide_id, "deleted": True},
    )
    return 0


# ============================================================================
# tm
# ============================================================================
def tm_import(args, emit) -> int:
    c = _client(args)
    data = _read_bytes(args.tmx)
    body = c.post(
        f"/api/projects/{args.project_id}/tm/import",
        json={
            "tmx_b64": base64.b64encode(data).decode(),
            "source": args.source,
            "target": args.target,
            "acknowledge_placeholder_loss": bool(
                getattr(args, "acknowledge_placeholder_loss", False)
            ),
        },
    )
    n = body["imported"]
    skipped_ref = body.get("skipped_refusal", 0)
    emit.result(
        f"Imported {n} TM entr{'y' if n == 1 else 'ies'} "
        f"({args.source} -> {args.target})"
        + (f", skipped {skipped_ref} refusal(s)" if skipped_ref else "")
        + (
            f", withheld {body['skipped_integrity']} for placeholder/tag integrity"
            if body.get("skipped_integrity")
            else ""
        )
        + (
            f", could not read {body['skipped_unreadable']} SDLTM unit(s)"
            if body.get("skipped_unreadable")
            else ""
        )
        + "."
        + (
            f" Written in {body['write_chunks']} short transactions (a failure part way keeps"
            " the rows already stored; re-run the same file to finish)."
            if body.get("write_chunks", 0) > 1
            else ""
        )
        + (
            f" Did not restore {body['concurrent_deleted']} TM entr"
            f"{'y' if body['concurrent_deleted'] == 1 else 'ies'} someone deleted while the"
            " import ran (import the file again to store them)."
            if body.get("concurrent_deleted")
            else ""
        ),
        {"ok": True, **body},
    )
    if not emit.as_json:
        for r in body.get("skipped_rows") or []:
            line = (
                f"  withheld unit {r['row']} [{r['code']}]: {r['reason']} (source: {r['source']})"
            )
            emit.say(line.encode("ascii", "backslashreplace").decode("ascii"))
        if body.get("skipped_integrity") and not body.get("acknowledged_placeholder_loss"):
            emit.say("  Re-run with --acknowledge-placeholder-loss to import placeholder losses.")
    return 0


def tm_export(args, emit) -> int:
    c = _client(args)
    body = c.get(
        f"/api/projects/{args.project_id}/tm/export",
        params={"source": args.source, "target": args.target},
    )
    xml = base64.b64decode(body["tmx_b64"])
    _write_bytes(args.tmx, xml, args.force)
    # Report the EFFECTIVE locales the server queried (the /api route echoes the coerced
    # region/case spelling in `source`/`target`; older servers omit them, so fall back to
    # the requested pair) — parity with the in-process arm (FINDING 5 read-back).
    eff_source = body.get("source", args.source)
    eff_target = body.get("target", args.target)
    emit.result(
        f"Wrote TMX -> {args.tmx} ({eff_source} -> {eff_target}).",
        {
            "ok": True,
            "path": args.tmx,
            "bytes": len(xml),
            "count": body.get("count"),
            "source": eff_source,
            "target": eff_target,
            "requested_source": args.source,
            "requested_target": args.target,
        },
    )
    return 0


def tm_list(args, emit) -> int:
    c = _client(args)
    params = _tm_query_params(args)
    params.update({"page": args.page, "per_page": args.per_page, "order": args.order})
    body = c.get("/api/tm", params=params)
    if emit.as_json:
        emit.json({"ok": True, **body})
    elif body.get("needs_customer"):
        names = ", ".join(c["name"] for c in body.get("customers", []))
        emit.say(
            "TM is organized by customer -- pass --customer <id> to browse one. "
            + (f"Available: {names}" if names else "No customers available.")
        )
    else:
        emit.table(
            [_tm_row(e) for e in body.get("entries", [])],
            headers=("id", "source", "target", "pair", "ctype", "uses", "last_used"),
        )
        emit.say(
            f"page {body.get('page')}/{body.get('pages') or 1} | {body.get('total')} entries total"
        )
    return 0


def tm_search(args, emit) -> int:
    c = _client(args)
    params = _tm_query_params(args, include_query=False)
    params.update({"q": args.text, "page": args.page, "per_page": args.per_page, "order": "recent"})
    body = c.get("/api/tm", params=params)
    if emit.as_json:
        emit.json({"ok": True, **body})
    elif body.get("needs_customer"):
        names = ", ".join(c["name"] for c in body.get("customers", []))
        emit.say(
            "TM is organized by customer -- pass --customer <id> to search one. "
            + (f"Available: {names}" if names else "No customers available.")
        )
    else:
        emit.table(
            [_tm_row(e) for e in body.get("entries", [])],
            headers=("id", "source", "target", "pair", "ctype", "uses", "last_used"),
        )
        emit.say(f"page {body.get('page')}/{body.get('pages') or 1} | {body.get('total')} matches")
    return 0


def tm_show(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/tm/{args.entry_id}")
    if emit.as_json:
        emit.json({"ok": True, **body})
    else:
        emit.say(f"TM entry #{body['id']} [{body['source_locale']}->{body['target_locale']}]")
        emit.say(f"  source: {body['source_text']}")
        emit.say(f"  target: {body['target_text']}")
        emit.say(
            f"  project: {body['project_id'] if body['project_id'] is not None else 'global'}"
            f"   origin: {body['origin']}"
        )
        emit.say(
            f"  created: {body['created_at']}   uses: {body['use_count']}"
            f"   last used: {body['last_used_at'] or 'never'}"
        )
    return 0


def tm_edit(args, emit) -> int:
    """Remote-CLI parity for `tm edit`: POST /api/tm/{id}/edit with the new target.
    The refusal-gate + not-found are enforced server-side (mirrors delete)."""
    c = _client(args)
    body = c.post(
        f"/api/tm/{args.entry_id}/edit",
        json={
            "target": args.target,
            "acknowledge_placeholder_loss": bool(
                getattr(args, "acknowledge_placeholder_loss", False)
            ),
        },
    )
    emit.result(f"Updated TM entry #{body.get('id', args.entry_id)} target.", {"ok": True, **body})
    return 0


def tm_stats(args, emit) -> int:
    c = _client(args)
    s = c.get("/api/tm/stats")
    if emit.as_json:
        emit.json({"ok": True, **s})
    else:
        emit.say(
            f"Total entries: {s['total']}   used: {s['used']}   "
            f"never used: {s['never_used']}   "
            f"total applications: {s['total_applications']}"
        )
        emit.say(
            f"Created range: {s['oldest_created_at'] or '-'} .. {s['newest_created_at'] or '-'}"
        )
        emit.say("By language pair:")
        emit.table(
            [(p["source_locale"] + "->" + p["target_locale"], p["count"]) for p in s["pairs"]],
            headers=("pair", "entries"),
        )
        # R-MED-8: the in-process arm also prints the Most-used table (the /api/tm/stats
        # body carries `top_used`); the remote arm was dropping it.
        if s.get("top_used"):
            emit.say("Most used:")
            emit.table(
                [
                    (
                        e["id"],
                        (e["source_text"] or "")[:40],
                        e["use_count"],
                        e["last_used_at"] or "-",
                    )
                    for e in s["top_used"]
                ],
                headers=("id", "source", "uses", "last_used"),
            )
    return 0


def tm_scrub_hedges(args, emit) -> int:
    c = _client(args)
    payload = {"apply": bool(args.apply)}
    if getattr(args, "tm_customer", None) is not None:
        payload["customer"] = args.tm_customer
    if getattr(args, "scrub_project", None) is not None:
        payload["project"] = args.scrub_project
    body = c.post("/api/tm/scrub-hedges", json=payload)
    hits = (body.get("segment") or []) + (body.get("full_text") or [])
    n = body.get("deleted", 0) if body.get("applied") else body.get("matched", 0)
    verb = "Deleted" if body.get("applied") else "Would delete"
    if hits and not emit.as_json:
        emit.table(
            [
                (
                    h["kind"],
                    h["id"],
                    h["pair"],
                    h["project_id"] if h.get("project_id") is not None else "global",
                    h["sample"],
                )
                for h in hits
            ],
            headers=("kind", "id", "pair", "project", "target[:80]"),
        )
    emit.result(
        f"{verb} {n} hedged TM entr{'y' if n == 1 else 'ies'}"
        + ("" if body.get("applied") else " (dry-run; re-run with --apply to remove)")
        + ".",
        {"ok": True, **body},
    )
    return 0


def tm_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if args.entry_id is not None:
        if args.dry_run:
            # A single-id dry run needs no server round-trip beyond a scoped fetch.
            c.get(f"/api/tm/{args.entry_id}")  # 404s cross-org, like the local arm
            emit.result(
                f"Would delete TM entry #{args.entry_id}.",
                {"ok": True, "dry_run": True, "count": 1, "entry_id": args.entry_id},
            )
            return 0
        if not confirm(emit, f"Delete TM entry #{args.entry_id}?", args.yes):
            raise CliError("aborted.", EXIT_USAGE)
        body = c.post(f"/api/tm/{args.entry_id}/delete", json={})
        emit.result(f"Deleted TM entry #{args.entry_id}.", {"ok": True, **body})
        return 0

    # Bulk delete by filter -- count-first, mirroring the local (in-process) arm:
    # a dry-run pass always computes the count BEFORE any real delete, and a real
    # delete is confirmed (or --yes) with that count shown, never silently.
    payload = _tm_query_params(args)
    if args.dry_run:
        payload["dry_run"] = True
        body = c.post("/api/tm/bulk-delete", json=payload)
        n = body.get("count", 0)
        emit.result(
            f"Would delete {n} TM entr{'y' if n == 1 else 'ies'}.",
            {"ok": True, "dry_run": True, "count": n},
        )
        return 0

    preview_payload = dict(payload)
    preview_payload["dry_run"] = True
    n = c.post("/api/tm/bulk-delete", json=preview_payload).get("count", 0)
    if n == 0:
        emit.result(
            "No TM entries match the filters -- nothing to delete.", {"ok": True, "count": 0}
        )
        return 0
    # --customer scopes the delete but is not a filter: customer-only still wipes that
    # customer's whole TM, so it still needs confirm_all.
    no_filters = not any(v for k, v in payload.items() if k != "customer")
    if no_filters:
        # Filterless bulk delete would wipe the caller's entire visible TM -- the
        # server now refuses this without confirm_all:true (MEDIUM finding fix).
        # --yes skips the interactive prompt (which is where the count would
        # normally be shown), so print the pending count BEFORE deleting.
        emit.warn(
            f"about to delete ALL {n} visible TM entr{'y' if n == 1 else 'ies'} (no filter given)."
        )
    if not confirm(emit, f"Delete {n} TM entr{'y' if n == 1 else 'ies'}?", args.yes):
        raise CliError("aborted.", EXIT_USAGE)
    payload["dry_run"] = False
    if no_filters:
        payload["confirm_all"] = True
    body = c.post("/api/tm/bulk-delete", json=payload)
    n = body.get("count", 0)
    emit.result(f"Deleted {n} TM entr{'y' if n == 1 else 'ies'}.", {"ok": True, "count": n})
    return 0


# ============================================================================
# glossary
# ============================================================================
def glossary_import(args, emit) -> int:
    c = _client(args)
    data = _read_bytes(args.csv)
    # The bytes travel unchanged (csv_b64 is the historical field name and now
    # carries CSV *or* TBX); `filename` is the extension hint so the SERVER applies
    # the same .tbx rule the local arm does. An older server ignores it and falls
    # back to sniffing the content, so a TBX still lands in the TBX parser.
    body = c.post(
        f"/api/projects/{args.project_id}/glossary/import",
        json={
            "csv_b64": base64.b64encode(data).decode(),
            "filename": os.path.basename(args.csv or ""),
            "source": args.source,
            "target": args.target,
        },
    )
    from .cmd_project_project import _chunks_note, _report_note

    skipped = body.get("skipped") or 0
    emit.result(
        f"Imported glossary: {body['entries']} entr"
        f"{'y' if body['entries'] == 1 else 'ies'}, "
        f"{body['senses']} sense(s)."
        + (
            f" Skipped {skipped} entr{'y' if skipped == 1 else 'ies'} with "
            "no matching language pair."
            if skipped
            else ""
        )
        + _chunks_note(body.get("write_chunks"), body.get("concurrent_deleted"))
        + _report_note(body),
        {"ok": True, **body},
    )
    return 0


def glossary_add(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/projects/{args.project_id}/glossary",
        json={
            "source_term": args.source_term,
            "target_term": args.target_term,
            "source": args.source,
            "target": args.target,
            "dnt": args.dnt,
        },
    )
    emit.result(
        f"Added term '{body['source_term']}' -> '{body['target_term']}' "
        f"on project #{args.project_id}.",
        {"ok": True, **body},
    )
    return 0


def glossary_list(args, emit) -> int:
    from .cmd_project import _note_truncated, _resolve_window

    c = _client(args)
    # AU-7/L6: forward the SQL paging window so --remote pages the
    # same way the local arm does, and surfaces truncation rather than silently cutting.
    limit, offset = _resolve_window(args)
    params = {"limit": limit, "offset": offset}
    if args.cursor is not None:
        params["cursor"] = args.cursor
    body = c.get(f"/api/projects/{args.project_id}/glossary", params=params)
    entries = body.get("entries", [])
    truncated = bool(body.get("truncated"))
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "entries": entries,
                "truncated": truncated,
                "limit": limit,
                "offset": offset,
                "next_cursor": body.get("next_cursor"),
            }
        )
    else:
        emit.table(
            [
                (
                    e["id"],
                    e["source_term"],
                    e["target_term"],
                    f"{e['source_locale']}->{e['target_locale']}",
                    "yes" if e["dnt"] else "",
                    e["senses"],
                )
                for e in entries
            ],
            headers=("id", "source", "target", "locales", "dnt", "senses"),
        )
        if args.cursor is not None and truncated and body.get("next_cursor") is not None:
            emit.warn(f"More terms: pass --cursor {body['next_cursor']} (reset --offset to 0).")
        else:
            _note_truncated(emit, truncated, offset, limit)
    return 0


def glossary_export(args, emit) -> int:
    """--remote arm of the LEGACY per-project `glossary export` (R-MED-11): GET
    /api/projects/{pid}/glossary/export?format=... returns RAW BYTES (Response, not the
    csv_b64 JSON the MODULE export uses), so read them via c.download. The route was
    added later than the CLI arm, so this arm was refusing with require_local + a stale
    'NO /api route' comment -- now corrected. Byte-identical output to the local arm."""
    from app.kernel import glossary as gloss_engine

    c = _client(args)
    fmt = gloss_engine.export_format_for(args.csv)
    resp = c.download(f"/api/projects/{args.project_id}/glossary/export", params={"format": fmt})
    data = resp.content
    _write_bytes(args.csv, data, args.force)
    emit.result(
        f"Wrote glossary {fmt.upper()} -> {args.csv}.",
        {
            "ok": True,
            "path": args.csv,
            "bytes": len(data),
            "format": fmt,
            "project_id": args.project_id,
        },
    )
    return 0


def gmod_create(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/glossaries",
        json={
            "name": args.name,
            "customer_id": getattr(args, "customer", None),
            "content_type_id": getattr(args, "content_type_id", None),
        },
    )
    emit.result(f"Created glossary #{body['id']} '{body['name']}' (draft).", {"ok": True, **body})
    return 0


def gmod_list(args, emit) -> int:
    c = _client(args)
    params = {}
    if getattr(args, "unassigned", False):
        params["unassigned"] = 1
    elif getattr(args, "customer", None) is not None:
        params["customer"] = args.customer
    body = c.get("/api/glossaries", params=params or None)
    gls = body.get("glossaries", [])
    if emit.as_json:
        emit.json({"ok": True, "glossaries": gls})
    else:
        emit.table(
            [_gmod_row(d) for d in gls],
            headers=("id", "name", "status", "customer", "pairs", "terms"),
        )
    return 0


def gmod_set_customer(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/glossaries/{args.glossary_id}/set-customer", json={"customer_id": args.customer_id}
    )
    emit.result(
        f"Assigned glossary #{body['id']} '{body['name']}' to customer {args.customer_id}.",
        {"ok": True, **body},
    )
    return 0


def gmod_set_content_type(args, emit) -> int:
    c = _client(args)
    ct_id = None if getattr(args, "clear", False) else getattr(args, "content_type_id", None)
    body = c.post(
        f"/api/glossaries/{args.glossary_id}/content-type", json={"content_type_id": ct_id}
    )
    emit.result(
        (
            f"Cleared content type on glossary #{body['id']}."
            if body.get("content_type_id") is None
            else f"Set content type {body['content_type_id']} on glossary #{body['id']}."
        ),
        {"ok": True, **body},
    )
    return 0


def gmod_show(args, emit) -> int:
    c = _client(args)
    params = {}
    if any(getattr(args, k, None) is not None for k in ("limit", "offset", "cursor")):
        from .cmd_project import _resolve_window

        limit, offset = _resolve_window(args)
        params = {"limit": limit, "offset": offset}
        if args.cursor is not None:
            params["cursor"] = args.cursor
    body = c.get(f"/api/glossaries/{args.glossary_id}", params=params)
    terms = body.get("terms", [])
    if emit.as_json:
        emit.json({"ok": True, **body})
        return 0
    emit.say(
        f"Glossary #{body['id']} '{body['name']}' [{body['status']}]"
        + (f" approved by {body['approved_by']}" if body.get("approved_by") else "")
    )
    emit.say(
        "  pairs: "
        + (
            ", ".join(
                f"{p['source_locale']}->{p['target_locale']}" for p in body.get("pairs") or []
            )
            or "-"
        )
    )
    # The meta column must be the SAME abbreviated summary the in-process arm prints
    # (pos/def/usex/subj/note) -- spelling the raw field names here made the human
    # `glossary module show --remote` table differ from the local one for identical
    # data. Deferred import: cmd_project imports this module at load time.
    from .cmd_project import _meta_summary

    emit.table(
        [
            (
                t["id"],
                t["source_term"],
                t["target_term"],
                f"{t['source_locale']}->{t['target_locale']}",
                "yes" if t["dnt"] else "",
                len(t.get("senses") or []),
                _meta_summary(t.get("meta")),
            )
            for t in terms
        ],
        headers=("id", "source", "target", "locales", "dnt", "senses", "meta"),
    )
    if body.get("truncated"):
        emit.warn(f"More terms: pass --cursor {body['next_cursor']} (reset --offset to 0).")
    return 0


def gmod_approve(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/glossaries/{args.glossary_id}/approve")
    emit.result(f"Approved glossary #{body['id']} '{body['name']}'.", {"ok": True, **body})
    return 0


def gmod_unapprove(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/glossaries/{args.glossary_id}/unapprove")
    emit.result(f"Reverted glossary #{body['id']} '{body['name']}' to draft.", {"ok": True, **body})
    return 0


def gmod_set_pairs(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/glossaries/{args.glossary_id}/pairs", json={"pairs": args.pair})
    emit.result(
        f"Set {len(body.get('pairs') or [])} pair(s) on glossary #{args.glossary_id}.",
        {"ok": True, **body},
    )
    return 0


def gmod_add_term(args, emit) -> int:
    c = _client(args)
    meta = _term_meta_from_args(args) or None
    body = c.post(
        f"/api/glossaries/{args.glossary_id}/terms",
        json={
            "source_term": args.source_term,
            "target_term": args.target_term,
            "source": args.source,
            "target": args.target,
            "dnt": args.dnt,
            "meta": meta,
        },
    )
    emit.result(
        f"Added term '{body['source_term']}' -> '{body['target_term']}' "
        f"to glossary #{args.glossary_id}.",
        {"ok": True, **body},
    )
    return 0


def gmod_edit_term(args, emit) -> int:
    c = _client(args)
    payload = {}
    if getattr(args, "target_term", None) is not None:
        payload["target_term"] = args.target_term
    if getattr(args, "dnt", None) is not None:
        payload["dnt"] = args.dnt
    if getattr(args, "case_sensitive", None) is not None:
        payload["case_sensitive"] = args.case_sensitive
    provided = _term_meta_from_args(args)
    if provided:
        # MERGE onto the term's EXISTING meta, exactly like the in-process arm
        # (cmd_project._gmod_edit_term). The /api edit-term route REPLACES a term's
        # metadata (the web row editor posts every field at once), but the CLI edits
        # ONE field at a time -- so posting only the provided flags silently WIPED
        # definition/part-of-speech/usage/domain/notes over --remote while the
        # byte-identical local command preserved them. Read the current meta first
        # (the show route carries the full `terms` array) and merge.
        # #530: the show route is WINDOWED (LIST_HARD_CAP terms a page), so a term past
        # the first page was never seen and its meta was replaced by only the provided
        # flags. Page (offset) until the term is found or the glossary is exhausted.
        cur = {}
        offset = 0
        while True:
            page = c.get(
                f"/api/glossaries/{args.glossary_id}",
                params={"offset": offset} if offset else None,
            )
            found = False
            terms = page.get("terms") or []
            for t in terms:
                if t.get("id") == args.entry_id:
                    cur = t.get("meta") or {}
                    found = True
                    break
            if found or not page.get("truncated") or not terms:
                break
            offset += len(terms)
        payload["meta"] = {**cur, **provided}
    body = c.post(f"/api/glossaries/{args.glossary_id}/terms/{args.entry_id}/edit", json=payload)
    emit.result(
        f"Updated term #{body['entry_id']} ('{body['source_term']}' -> "
        f"'{body['target_term']}') in glossary #{args.glossary_id}.",
        {"ok": True, **body},
    )
    return 0
