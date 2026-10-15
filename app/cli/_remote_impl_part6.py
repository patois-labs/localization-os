"""_remote_impl: part 6 (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import os

from app.cli._common import (
    EXIT_USAGE,
    CliError,
)
from app.cli._remote_impl_common import _client, _q_row_line, _read_bytes, _write_bytes


def keys_changes(args, emit) -> int:
    from ._common import EXIT_USAGE, CliError
    from .cmd_keys import emit_changes, persist_cursor, resolve_cursor

    c = _client(args)
    # The mode guard already ran in cmd_keys._keys_changes before dispatch here.
    modified_after = getattr(args, "modified_after", None)
    if modified_after is not None:
        # M6: validate the timestamp LOCALLY first so an unparseable value exits 2
        # (client-input error) WITHOUT a round-trip -- identical to the in-process arm,
        # not the exit-1 a server 400 would give. Uses the cursor parser so the opaque
        # `ISO~id` token (NEW-3) validates too.
        from app.kernel import keys as keys_svc

        try:
            keys_svc.parse_modified_after_cursor(modified_after)
        except keys_svc.BadTimestampError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # MODIFIED-AFTER mode: no cursor file (the server keyset-pages, not a stored
        # cursor). Byte-identical rendering to the in-process arm.
        params = {"modified_after": modified_after}
        if getattr(args, "locale", None):
            params["locale"] = args.locale
        if getattr(args, "after_id", None) is not None:
            params["after_id"] = args.after_id
        if args.limit:
            params["limit"] = args.limit
        body = c.get(f"/api/projects/{args.project_id}/changes", params=params)
        emit_changes(emit, {k: v for k, v in body.items() if k != "ok"})
        return 0
    since, path = resolve_cursor(args, emit)
    params = {"since": since}
    if args.limit:
        params["limit"] = args.limit
    body = c.get(f"/api/projects/{args.project_id}/changes", params=params)
    payload = {k: v for k, v in body.items() if k != "ok"}
    emit_changes(emit, payload)
    persist_cursor(path, args.project_id, payload)
    return 0


# --- per-string in-context images (Batch 14 T1): the --remote twins ----------
# The four `keys image` leaves honor --remote like every other keys leaf (web+CLI
# parity), each hitting the /api/keys/{key_id}/images route family the MCP tools + web
# already drive. `add` uploads multipart (the /api attach's file branch); the others are
# id-addressed so no key lookup is needed.
def keys_image_add(args, emit) -> int:
    from pathlib import Path

    from app import config

    from ._common import EXIT_USAGE, CliError

    src = Path(str(args.path)).expanduser()
    if not src.is_file():
        raise CliError(f"no such file: {src}", EXIT_USAGE)
    data = src.read_bytes()
    if len(data) > config.KEY_IMAGE_MAX_BYTES:
        raise CliError("that image is too large.", EXIT_USAGE)
    c = _client(args)
    form = {"caption": args.caption} if getattr(args, "caption", "") else None
    body = c.upload(f"/api/keys/{int(args.key_id)}/images", src.name, data, form=form)
    img = body.get("image", {})
    emit.result(
        f"Attached image #{img.get('id')} to key #{int(args.key_id)}.",
        {"ok": True, "image": img},
    )
    return 0


def keys_image_list(args, emit) -> int:
    from .cmd_keys import image_lines

    c = _client(args)
    body = c.get(f"/api/keys/{int(args.key_id)}/images")
    views = body.get("images", [])
    human = (
        f"Key #{int(args.key_id)} has {len(views)} image(s):\n" + "\n".join(image_lines(views))
        if views
        else f"Key #{int(args.key_id)} has no images."
    )
    emit.result(human, {"ok": True, "key_id": int(args.key_id), "images": views})
    return 0


def keys_image_rm(args, emit) -> int:
    c = _client(args)
    c.delete(f"/api/keys/{int(args.key_id)}/images/{int(args.image_id)}")
    emit.result(
        f"Deleted image #{int(args.image_id)} from key #{int(args.key_id)}.",
        {"ok": True, "id": int(args.image_id), "deleted": True},
    )
    return 0


def keys_image_set_primary(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/keys/{int(args.key_id)}/images/{int(args.image_id)}/primary")
    emit.result(
        f"Image #{int(args.image_id)} is now the primary for key #{int(args.key_id)}.",
        {"ok": True, "image": body.get("image", {})},
    )
    return 0


# ============================================================================
# admin audit — the --remote arms of `admin audit export|verify` (AP-7). Both were
# refusing --remote (require_local, exit 2) despite the /api twins existing; wired
# now to GET /api/audit/export (NDJSON stream) and GET /api/audit/verify, emitting
# the SAME human/--json shape as the in-process arms so output is transport-neutral.
# The server self-logs the export on its side (api_cli.api_audit_export), so the
# remote arm does not double-log.
# ============================================================================
def audit_export(args, emit) -> int:
    import json
    import sys

    c = _client(args)
    # M6: validate --since LOCALLY first so a garbage value exits 2 (client-input error)
    # without a round-trip -- identical to the local arm's _parse_since, not the exit-1 a
    # server 400 gives.
    if getattr(args, "since", ""):
        from .cmd_admin import _parse_since

        _parse_since(args.since)  # raises CliError(EXIT_USAGE) on a bad timestamp
    params = {}
    if args.since:
        params["since"] = args.since
    if getattr(args, "filter_actor", ""):
        params["actor"] = args.filter_actor
    if getattr(args, "action", ""):
        params["action"] = args.action
    if getattr(args, "customer_id", None):
        params["customer_id"] = args.customer_id
    resp = c.download("/api/audit/export", params=params)
    # L7: split on "\n" ONLY (NOT splitlines) so a value carrying U+2028/U+2029 -- which
    # json.dumps(ensure_ascii=False) leaves raw and str.splitlines() would treat as a
    # line break -- never fractures a record. json.dumps escapes real newlines, so a
    # record never contains a bare "\n"; this split is therefore lossless.
    raw = resp.content
    lines = [ln for ln in raw.decode("utf-8").split("\n") if ln.strip()]
    if args.out:
        # Write the server's bytes VERBATIM (payload-exact), not a re-encoded rejoin.
        _write_bytes(args.out, raw, args.force)
        emit.result(
            f"Wrote audit export -> {args.out} ({len(lines)} record(s)).",
            {"ok": True, "out": args.out, "records": len(lines)},
        )
    elif emit.as_json:
        emit.json({"ok": True, "records": len(lines), "rows": [json.loads(ln) for ln in lines]})
    else:
        # Byte-verbatim to stdout when possible (preserves the payload exactly);
        # fall back to text for a wrapped/!buffer stdout.
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(raw)
            buf.flush()
        else:
            sys.stdout.write(raw.decode("utf-8"))
    return 0


def audit_verify(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/audit/verify")
    checked = body.get("checked")
    first_bad = body.get("first_bad_id")
    # Mirror cmd_db.verify_audit_chain's output exactly (the local arm's core).
    if body.get("valid"):
        emit.result(
            f"OK: audit chain verified ({checked} row(s)).",
            {"ok": True, "checked": checked, "first_bad_id": None},
        )
        return 0
    where = f" (first bad row id: {first_bad})" if first_bad is not None else ""
    emit.result(
        f"BROKEN: audit chain failed after {checked} row(s){where}.",
        {"ok": False, "checked": checked, "first_bad_id": first_bad},
    )
    raise CliError("audit chain verification failed.", 1)


# ============================================================================
# Wave R1b content parity: workflow clone/copy/author, workflow groups, prompt
# templates, project templates, image upload/translate, context packages,
# segmentation profiles. Each is the HTTP twin of an in-process arm and rebuilds
# the SAME human/--json shape off the frozen /api response body.
# ============================================================================


# --- workflow clone / copy / author ------------------------------------------
def workflow_clone(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/workflows/{args.workflow_id}/clone", json={})
    emit.result(
        f"Cloned workflow #{body['source_id']} -> #{body['id']}: {body['name']} "
        f"({body['steps']} step(s)).",
        {
            "ok": True,
            "id": body["id"],
            "name": body["name"],
            "steps": body["steps"],
            "source_id": body["source_id"],
        },
    )
    return 0


def workflow_copy(args, emit) -> int:
    c = _client(args)
    res = c.post(
        f"/api/workflows/{args.workflow_id}/copy",
        json={"to_pairs": args.to_pairs, "adapt": args.adapt},
    )
    lines = [f"Copied workflow #{args.workflow_id} into group #{res['group_id']}:"]
    for item in res.get("created") or []:
        lines.append(
            f"  -> #{item['workflow_id']} {item['pair']}"
            f"{' (adapted)' if item.get('adapted') else ''}"
        )
        for w in item.get("warnings") or []:
            lines.append(f"     ! {w}")
    for sk in res.get("skipped") or []:
        lines.append(f"  -- skipped {sk['pair']}: {sk['reason']} (nothing was replaced)")
    if not (res.get("created")):
        lines.append("No new variants were created.")
    if args.adapt:
        lines.append(
            "REMINDER: LLM-adapted prompts are marked needs_review -- a "
            "linguist should post-edit each variant before activating it."
        )
    emit.result("\n".join(lines), {"ok": True, **res})
    return 0


def workflow_author(args, emit) -> int:
    """Remote subset of `workflow author`: the server does a SINGLE apply (no
    --auto-fix repair loop; that is in-process only). Draft-only or created summary."""
    c = _client(args)
    # R-LOW: --auto-fix is an IN-PROCESS-only repair loop; warn rather than silently
    # dropping it (the server does a single apply). To stderr, so --json is unaffected.
    if getattr(args, "auto_fix", None) is not None:
        emit.warn(
            "--auto-fix is ignored over --remote: the server does a single apply "
            "(the iterative repair loop is in-process only)."
        )
    if getattr(args, "adapt", False):
        emit.warn("--adapt is ignored: a designed workflow has no language variants")
    body = c.post(
        "/api/workflows/author",
        json={
            "description": args.description,
            "provider": args.provider,
            "model": getattr(args, "model", ""),
            "create": args.create,
        },
    )
    warnings = body.get("warnings") or []
    if body.get("created"):
        for w in warnings:
            emit.warn(w)
        emit.result(
            f"Created workflow #{body['id']}: {body['name']} ({body['steps']} step(s)).",
            {
                "ok": True,
                "id": body["id"],
                "name": body["name"],
                "steps": body["steps"],
                "warnings": warnings,
            },
        )
        return 0
    fields = body.get("draft") or {}
    if emit.as_json:
        emit.json({"ok": True, "draft": fields, "warnings": warnings})
        return 0
    emit.say("Drafted workflow setup:")
    emit.say(f"  name: {fields.get('name') or '(none)'}")
    emit.say(f"  customer: {fields.get('customer_name') or '(none)'}")
    steps = fields.get("steps") or []
    emit.say(f"  steps ({len(steps)}):")
    for idx, s in enumerate(steps, 1):
        bits = [s.get("step_type")]
        if s.get("provider"):
            bits.append(s["provider"])
        if s.get("qe_gate"):
            bits.append(f"qe_gate>={s.get('qe_threshold')}")
        # feat/lqa-configurable-threshold: an lqa step's configured pass bar (numeric
        # floor only -- a major/critical MQM error routes regardless of score, Q7).
        if s.get("lqa_threshold") is not None:
            bits.append(f"lqa-floor>={s.get('lqa_threshold')}")
        if s.get("glossary_name"):
            bits.append(f"glossary:{s['glossary_name']}")
        emit.say(f"    {idx}. {s.get('name') or '(unnamed)'} [{', '.join(b for b in bits if b)}]")
    if fields.get("notes_for_reviewer"):
        emit.say(f"  notes: {fields['notes_for_reviewer']}")
    if warnings:
        emit.say("WARNINGS:")
        for w in warnings:
            emit.say(f"  - {w}")
    return 0


# --- workflow groups ---------------------------------------------------------
def workflow_group_create(args, emit) -> int:
    c = _client(args)
    payload = {"name": args.name, "description": args.description}
    if str(getattr(args, "customer_id", "") or "").strip():
        payload["customer_id"] = args.customer_id
    body = c.post("/api/workflow-groups", json=payload)
    cust = body.get("customer_id")
    emit.result(
        f"Created workflow group #{body['id']}: {body['name']}"
        f"{' (customer ' + str(cust) + ')' if cust else ' (global)'}.",
        {"ok": True, "id": body["id"], "name": body["name"], "customer_id": cust},
    )
    return 0


def workflow_group_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/workflow-groups")
    groups = body.get("groups", [])
    if emit.as_json:
        emit.json({"ok": True, "groups": groups})
    else:
        emit.table(
            [
                (g["id"], g["name"], g.get("customer_id") or "global", g.get("variants"))
                for g in groups
            ],
            headers=("id", "name", "customer", "variants"),
        )
    return 0


def workflow_group_show(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/workflow-groups/{args.group_id}")
    variants = body.get("variants", [])
    vrows = [
        {
            "workflow_id": v["workflow_id"],
            "name": v["name"],
            "source": v.get("source_locale"),
            "target": v.get("target_locale"),
            "default": v.get("is_default"),
        }
        for v in variants
    ]
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "id": body["id"],
                "name": body["name"],
                "customer_id": body.get("customer_id"),
                "variants": vrows,
            }
        )
    else:
        cust = body.get("customer_id")
        emit.say(
            f"Group #{body['id']}: {body['name']} "
            f"({'customer ' + str(cust) if cust else 'global'}, "
            f"{len(vrows)} variant(s))"
        )
        emit.table(
            [
                (
                    v["workflow_id"],
                    v["name"],
                    (v["source"] or "*") + "->" + (v["target"] or "*"),
                    "yes" if v["default"] else "",
                )
                for v in vrows
            ],
            headers=("wf_id", "name", "pair", "default"),
        )
    return 0


def workflow_group_add(args, emit) -> int:
    c = _client(args)
    payload = {
        "workflow_id": args.workflow_id,
        "make_default": args.make_default,
        "force": args.force,
    }
    if str(getattr(args, "pair", "") or "").strip():
        payload["pair"] = args.pair
    body = c.post(f"/api/workflow-groups/{args.group_id}/add", json=payload)
    pair = (body.get("source_locale") or "*") + "->" + (body.get("target_locale") or "*")
    emit.result(
        f"Added workflow #{body['workflow_id']} to group #{body['group_id']} "
        f"as variant {pair}{' (default)' if body.get('is_default') else ''}.",
        {
            "ok": True,
            "workflow_id": body["workflow_id"],
            "group_id": body["group_id"],
            "source_locale": body.get("source_locale"),
            "target_locale": body.get("target_locale"),
            "is_default": body.get("is_default"),
        },
    )
    return 0


def workflow_group_set_default(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/workflow-groups/{args.group_id}/default", json={"workflow_id": args.workflow_id}
    )
    pair = (body.get("source_locale") or "*") + "->" + (body.get("target_locale") or "*")
    emit.result(
        f"Workflow #{body['workflow_id']} ({pair}) is now the default variant of "
        f"group #{body['group_id']}.",
        {
            "ok": True,
            "workflow_id": body["workflow_id"],
            "group_id": body["group_id"],
            "is_default": body.get("is_default"),
            "source_locale": body.get("source_locale"),
            "target_locale": body.get("target_locale"),
        },
    )
    return 0


# --- reusable prompt templates -----------------------------------------------
def prompt_template_create(args, emit) -> int:
    c = _client(args)
    payload = {"name": args.name, "body": args.body}
    if getattr(args, "customer_id", None) is not None:
        payload["customer_id"] = args.customer_id
    body = c.post("/api/prompt-templates", json=payload)
    cust = body.get("customer_id")
    emit.result(
        f"Created prompt template #{body['id']}: {body['name']} "
        f"({body.get('scope')}{', customer #%d' % cust if cust else ''}).",  # noqa: UP031
        {
            "ok": True,
            "id": body["id"],
            "name": body["name"],
            "scope": body.get("scope"),
            "customer_id": cust,
        },
    )
    return 0


def prompt_template_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/prompt-templates")
    rows = body.get("prompt_templates", [])
    if emit.as_json:
        emit.json({"ok": True, "prompt_templates": rows})
    else:
        emit.table(
            [
                (
                    r["id"],
                    r["name"],
                    r["scope"] if not r.get("customer_id") else "customer #%d" % r["customer_id"],  # noqa: UP031
                    r.get("pinned_by"),
                )
                for r in rows
            ],
            headers=("id", "name", "scope", "pinned_by"),
        )
    return 0


def system_prompt_list(args, emit) -> int:
    """`workflow prompt system` over /api (GitHub #356, catalogue + editable heads)."""
    c = _client(args)
    body = c.get("/api/system-prompts")
    return _emit_system_prompts(args, emit, body)


def system_prompt_detail(args, emit) -> int:
    """`workflow prompt system-history` over /api."""
    c = _client(args)
    body = c.get(f"/api/system-prompts/{_sp_key(args)}")
    return _emit_system_prompt_history(args, emit, body["prompt"])


def system_prompt_set(args, emit, text: str) -> int:
    """`workflow prompt system-set` over /api (admin; the server enforces role and flag)."""
    c = _client(args)
    out = c.put(f"/api/system-prompts/{_sp_key(args)}", json={"body": text})
    return _emit_system_prompt_saved(args, emit, out, "Saved")


def system_prompt_reset(args, emit) -> int:
    """`workflow prompt system-reset` over /api."""
    c = _client(args)
    out = c.delete(f"/api/system-prompts/{_sp_key(args)}")
    return _emit_system_prompt_saved(args, emit, out, "Reset")


def system_prompt_restore(args, emit) -> int:
    """`workflow prompt system-restore` over /api."""
    c = _client(args)
    out = c.post(
        f"/api/system-prompts/{_sp_key(args)}/restore", json={"version": int(args.version)}
    )
    return _emit_system_prompt_saved(args, emit, out, "Restored")


def _sp_key(args) -> str:
    from urllib.parse import quote

    return quote(str(args.key), safe="")


def _emit_system_prompts(args, emit, body) -> int:
    """Shared by the in-process and --remote arms so both print the same thing. `body` is the
    `system_prompts.catalogue_payload` shape (catalogue rows + the editable heads)."""
    from app.cli._common import EXIT_USAGE, CliError

    rows = body.get("system_prompts", [])
    editable = body.get("editable", [])
    key = getattr(args, "key", None)
    if key:
        hit = [r for r in rows if r["key"] == key]
        if hit:
            row = hit[0]
            lines = [row["name"], row["used_in"]]
            if row.get("note"):
                lines.append(row["note"])
            lines += ["", "Built-in text:", row["text"]]
            if row.get("override_version"):
                lines += [
                    "",
                    f"What this org's runs send (override version {row['override_version']}):",
                    row["effective_text"],
                ]
            emit.result("\n".join(lines), {"ok": True, "system_prompts": [row]})
            return 0
        hit_e = [e for e in editable if e["key"] == key]
        if hit_e:
            return _emit_editable_head(emit, hit_e[0], body.get("overrides_enabled", False))
        raise CliError(f"no system prompt with key {key!r}.", EXIT_USAGE)
    if emit.as_json:
        emit.json({"ok": True, **body})
    else:
        emit.table(
            [(r["key"], r["name"]) for r in rows],
            headers=("key", "name"),
        )
        emit.say("")
        emit.say(
            "Editable heads (org admins, when system_prompt_overrides_enabled is on; "
            "see `workflow prompt system-set`):"
        )
        emit.table(
            [
                (
                    e["key"],
                    e["name"],
                    f"override v{e['override']['version']}" if e.get("override") else "built-in",
                )
                for e in editable
            ],
            headers=("key", "name", "in force"),
        )
    return 0


def _emit_editable_head(emit, e, enabled) -> int:
    lines = [
        e["name"],
        e["used_in"],
        "",
        "Always added before your text (locked):",
        e["floor_before"],
        "",
        "Built-in text:",
        e["default_body"],
    ]
    if e.get("override"):
        lines += [
            "",
            f"Override in force (version {e['override']['version']}):",
            e["override"]["body"],
        ]
    lines += ["", "Always added after your text (locked):", e["floor_after"]]
    if e.get("floor_note"):
        lines += ["", e["floor_note"]]
    if not enabled:
        lines += ["", "Overrides are switched off (setting system_prompt_overrides_enabled=0)."]
    emit.result("\n".join(lines), {"ok": True, "overrides_enabled": enabled, "editable": [e]})
    return 0


def _emit_system_prompt_saved(args, emit, out, verb) -> int:
    """Shared success printer for set / reset / restore."""
    prompt = out.get("prompt") or {}
    key = prompt.get("key") or getattr(args, "key", "")
    if verb == "Reset" and not out.get("changed", True):
        if prompt.get("workflow_id") is not None:
            text = f"{key}: workflow #{prompt['workflow_id']} already inherits; nothing to reset."
        else:
            text = f"{key}: already the built-in text; nothing to reset."
    else:
        text = f"{verb} {key}: version {out.get('version')}."
    lines = [text] + [f"WARNING: {w}" for w in out.get("warnings") or []]
    emit.result("\n".join(lines), {"ok": True, **out})
    return 0


def _emit_system_prompt_history(args, emit, prompt) -> int:
    rows = prompt.get("history") or []
    if emit.as_json:
        emit.json({"ok": True, "prompt": prompt})
        return 0
    emit.say(f"{prompt['name']} ({prompt['key']})")
    if not rows:
        emit.say("No versions yet: the built-in text is in force.")
        return 0
    emit.table(
        [
            (
                r["version"],
                r["action"],
                r["created_by"] or "-",
                r["created_at"] or "-",
                len(r["body"]),
            )
            for r in rows
        ],
        headers=("version", "action", "by", "at", "chars"),
    )
    return 0


# --- project templates -------------------------------------------------------
def template_create(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/templates",
        json={
            "name": args.name,
            "project_id": args.project_id,
            "scope": args.scope,
            "description": args.description,
        },
    )
    tpl = body["template"]
    emit.result(
        f"Saved template #{tpl['id']}: {tpl['name']} "
        f"[{'customer' if tpl.get('customer_id') else 'global'}].",
        {"ok": True, "template": tpl},
    )
    return 0


def template_list(args, emit) -> int:
    # --all / --customer-id are LOCAL-OPERATOR views. The /api/templates route confines
    # visibility to the TOKEN's own scope (global + the caller's bound customer; a
    # foreign customer's is never returned -- the P4 isolation rule) and has no server
    # filter for either flag. Refuse rather than accept-and-ignore (R-MED-10): honouring
    # them over the wire would mean widening scope past the token, which the route
    # deliberately forbids.
    if getattr(args, "all", False):
        raise CliError(
            "`template list --all` is a local admin view; over --remote the token's own "
            "scope determines which templates are listed.",
            EXIT_USAGE,
        )
    if str(getattr(args, "customer_id", None) or "").strip():
        raise CliError(
            "`template list --customer-id` is local-only; over --remote a customer-bound "
            "token already lists its own customer's templates, and a foreign customer's "
            "are never returned.",
            EXIT_USAGE,
        )
    c = _client(args)
    body = c.get("/api/templates")
    rows = body.get("templates", [])
    if emit.as_json:
        emit.json({"ok": True, "templates": rows})
        return 0
    if not rows:
        emit.say("No templates.")
        return 0
    emit.table(
        [
            (
                r["id"],
                r["name"],
                r["scope"],
                r["source_locale"],
                ",".join(r["target_locales"]),
                r["default_provider"],
            )
            for r in rows
        ],
        headers=("id", "name", "scope", "source", "targets", "provider"),
    )
    return 0


def template_show(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/templates/{args.template_id}")
    r = body["template"]
    emit.result(
        f"#{r['id']} {r['name']} [{r['scope']}]\n"
        f"  {r['source_locale']} -> {', '.join(r['target_locales']) or '(none)'}\n"
        f"  provider: {r['default_provider']}"
        f"  workflow: {r['default_workflow_id'] or '-'}"
        f"  segmentation: {r['segmentation_profile_id'] or '-'}\n"
        f"  description: {r['description'] or '-'}",
        {"ok": True, "template": r},
    )
    return 0


def template_apply(args, emit) -> int:
    c = _client(args)
    payload = {"name": args.name}
    cust = getattr(args, "customer_id", None)
    if str(cust or "").strip():
        payload["customer_id"] = cust
    if getattr(args, "connected", False):
        payload["intake"] = "connected"
    body = c.post(f"/api/templates/{args.template_id}/apply", json=payload)
    warnings = body.get("warnings") or []
    for w in warnings:
        emit.warn(w)
    emit.result(
        f"Created project #{body['project_id']}: {body['name']} from template #{args.template_id}.",
        {
            "ok": True,
            "project_id": body["project_id"],
            "intake": body.get("intake", "upload"),
            "warnings": warnings,
        },
    )
    return 0


def template_rename(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/templates/{args.template_id}/rename", json={"name": args.name})
    tpl = body["template"]
    emit.result(f"Renamed template #{tpl['id']} to {tpl['name']}.", {"ok": True, "template": tpl})
    return 0


def template_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if not confirm(emit, f"Delete template #{args.template_id}?", getattr(args, "yes", False)):
        return EXIT_USAGE
    body = c.post(f"/api/templates/{args.template_id}/delete", json={})
    tid = body.get("id", args.template_id)
    emit.result(f"Deleted template #{tid}.", {"ok": True, "deleted": tid})
    return 0


# --- images (Beta) -----------------------------------------------------------
def image_upload(args, emit) -> int:
    c = _client(args)
    data = _read_bytes(args.file)
    filename = os.path.basename(args.file)
    body = c.upload("/api/images", filename, data, form={"project_id": str(args.project_id)})
    emit.result(
        f"Uploaded image #{body['document_id']}: {body.get('filename')} "
        f"({body.get('kind')}, {body.get('regions')} region(s)) into "
        f"project #{body['project_id']}.",
        {
            "ok": True,
            "id": body.get("id", body["document_id"]),
            "filename": body.get("filename"),
            "fmt": body.get("fmt"),
            "kind": body.get("kind"),
            "regions": body.get("regions"),
            "project_id": body["project_id"],
        },
    )
    return 0


def image_translate(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/images/{args.doc_id}/translate", json={"locale": args.locale}, paid=True)
    stats = body.get("stats") or {}
    emit.result(
        f"Translated image #{body['document_id']} into {body['locale']}: "
        f"{stats.get('translated', 0)} region segment(s) "
        f"({stats.get('from_segment_tm', 0)} from TM, "
        f"{stats.get('provider_calls', 0)} provider call(s), "
        f"{stats.get('errors', 0)} flagged/error).",
        {"ok": True, "doc_id": body["document_id"], "locale": body["locale"], "stats": stats},
    )
    return 0


# --- context packages --------------------------------------------------------
def context_show(args, emit) -> int:
    c = _client(args)
    params = {}
    if getattr(args, "project", None) is not None:
        params["project"] = args.project
    if getattr(args, "customer", None) is not None:
        params["customer"] = args.customer
    if getattr(args, "content_type", None) is not None:
        params["content_type"] = args.content_type
    if getattr(args, "document", None) is not None:
        params["document"] = args.document
    body = c.get("/api/context/resolve", params=params)
    effective = body.get("context") or {}
    provenance = body.get("provenance") or {}
    block = body.get("block")
    if emit.as_json:
        emit.json({"ok": True, "effective": effective, "provenance": provenance, "block": block})
        return 0
    if not effective:
        emit.say("No effective context for this scope.")
        return 0
    emit.say("Effective context package:")
    for field in sorted(effective):
        emit.say(f"  {field}: {effective[field]!r}  [from {provenance.get(field, '?')}]")
    emit.say("")
    emit.say("Rendered block:")
    emit.say(block or "(empty)")
    return 0


def context_set_profile(args, emit) -> int:
    from .cmd_context import _raw_from_args

    c = _client(args)
    payload = {"name": args.name, "context": _raw_from_args(args)}
    if getattr(args, "customer", None) is not None:
        payload["customer_id"] = args.customer
    if getattr(args, "content_type", None) is not None:
        payload["content_type_id"] = args.content_type
    if getattr(args, "project", None) is not None:
        payload["project_id"] = args.project
    body = c.post("/api/context/profiles", json=payload)
    row = body["profile"]
    emit.result(f"Saved context card #{row['id']} [{row['scope']}].", {"ok": True, "profile": row})
    return 0


def context_set_document(args, emit) -> int:
    from .cmd_context import _raw_from_args

    c = _client(args)
    body = c.post(f"/api/context/documents/{args.document}", json={"context": _raw_from_args(args)})
    stored = body.get("context")
    if stored:
        emit.result(
            f"Saved context override on document #{args.document}.",
            {"ok": True, "document": args.document, "context": stored},
        )
    else:
        emit.result(
            f"Cleared context override on document #{args.document}.",
            {"ok": True, "document": args.document, "context": None},
        )
    return 0


def context_ab(args, emit) -> int:
    c = _client(args)
    payload = {"document": args.document}
    if getattr(args, "locale", None) is not None:
        payload["locale"] = args.locale
    if getattr(args, "provider", None) is not None:
        payload["provider"] = args.provider
    if getattr(args, "max_segments", None) is not None:
        payload["max_segments"] = args.max_segments
    body = c.post("/api/context/ab", json=payload, paid=True)
    data = body["ab"]
    if emit.as_json:
        emit.json({"ok": True, "ab": data})
        return 0
    emit.say(
        "Context A/B measurement -- document #{} ({})".format(data["document_id"], data["locale"])
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
    emit.say("")
    if not data["context_present"]:
        emit.say("  (no context package resolved -- both arms identical)")
    on, off, delta = data["on"], data["off"], data["delta"]

    def _fmt(v, suffix=""):
        return "n/a" if v is None else (f"{v}{suffix}")

    emit.say("  Overall QE (mean, 0-100):")
    emit.say(
        "    context ON : %s  (scored %d, skipped %d)"  # noqa: UP031
        % (_fmt(on["mean_qe"]), on["scored"], on["skipped"])
    )
    emit.say(
        "    context OFF: %s  (scored %d, skipped %d)"  # noqa: UP031
        % (_fmt(off["mean_qe"]), off["scored"], off["skipped"])
    )
    emit.say("    delta      : {}".format(_fmt(delta["mean_qe"])))
    emit.say("")
    emit.say("  Terminology adherence (DNT preserved, 0-1):")
    emit.say(
        "    context ON : %s  (%d/%d preserved)"  # noqa: UP031
        % (
            _fmt(on["term_adherence"]),
            on["term_applicable"] - on["term_violations"],
            on["term_applicable"],
        )
    )
    emit.say(
        "    context OFF: %s  (%d/%d preserved)"  # noqa: UP031
        % (
            _fmt(off["term_adherence"]),
            off["term_applicable"] - off["term_violations"],
            off["term_applicable"],
        )
    )
    emit.say("    delta      : {}".format(_fmt(delta["term_adherence"])))
    emit.say("")
    emit.say("  VERDICT: {}".format(data["verdict"]))
    return 0


# --- segmentation profiles ---------------------------------------------------
def segprofile_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/segprofiles")
    rows = body.get("segprofiles", [])
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


def segprofile_show(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/segprofiles/{args.profile_id}")
    row = body["segprofile"]
    emit.result(
        f"#{row['id']} {row['name']} [{row['scope']}] "
        f"{'custom SRX' if row['custom'] else 'built-in rules'}"
        f"{' (system default)' if row['is_system_default'] else ''}\n"
        f"  description: {row['description'] or '(none)'}",
        {"ok": True, "segprofile": row},
    )
    return 0


def segprofile_create(args, emit) -> int:
    c = _client(args)
    payload = {
        "name": args.name,
        "description": args.description,
        "system_default": bool(args.system_default),
    }
    if (getattr(args, "file", "") or "").strip():
        data = _read_bytes(args.file)
        payload["srx_b64"] = base64.b64encode(data).decode()
    if getattr(args, "customer_id", None) is not None:
        payload["customer_id"] = args.customer_id
    body = c.post("/api/segprofiles", json=payload)
    prof = body["segprofile"]
    emit.result(
        f"Created segmentation profile #{prof['id']}: {prof['name']}.",
        {"ok": True, "profile": prof},
    )
    return 0


def segprofile_clone(args, emit) -> int:
    c = _client(args)
    payload = {"new_name": args.new_name}
    if getattr(args, "customer_id", None) is not None:
        payload["customer_id"] = args.customer_id
    body = c.post(f"/api/segprofiles/{args.profile_id}/clone", json=payload)
    prof = body["segprofile"]
    emit.result(f"Cloned to #{prof['id']}: {prof['name']}.", {"ok": True, "profile": prof})
    return 0


def segprofile_rename(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/segprofiles/{args.profile_id}/rename", json={"name": args.name})
    prof = body["profile"]  # this route returns "profile" (not "segprofile")
    emit.result(
        f"Renamed segmentation profile #{prof['id']} to {prof['name']}.",
        {"ok": True, "profile": prof},
    )
    return 0


def segprofile_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if not confirm(
        emit, f"Delete segmentation profile #{args.profile_id}?", getattr(args, "yes", False)
    ):
        return EXIT_USAGE
    body = c.post(f"/api/segprofiles/{args.profile_id}/delete", json={})
    pid = body.get("id", args.profile_id)
    emit.result(f"Deleted segmentation profile #{pid}.", {"ok": True, "deleted": pid})
    return 0


def segprofile_set_default(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/segprofiles/{args.profile_id}/set-default", json={"clear": bool(args.clear)}
    )
    prof = body["segprofile"]
    emit.result(
        ("Cleared system default from " if args.clear else "System default set to ")
        + f"#{prof['id']} ({prof['name']}).",
        {"ok": True, "profile": prof},
    )
    return 0


def segprofile_assign(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/segprofiles/assign", json={"project_id": args.project_id, "profile_id": args.profile}
    )
    tgt = body.get("segmentation_profile_id")
    emit.result(
        f"Project #{body['project_id']} segmentation override: "
        + (f"profile #{tgt}." if tgt else "cleared (inherit chain)."),
        {"ok": True, "project_id": body["project_id"], "segmentation_profile_id": tgt},
    )
    return 0


def query_open(args, emit) -> int:
    c = _client(args)
    body = {"subject": args.subject, "body": args.body}
    if args.assignee:
        body["assignee_email"] = args.assignee
    if args.segment is not None:
        body["segment_id"] = args.segment
    elif args.key is not None:
        body["key_id"] = args.key
    elif args.document is not None:
        body["document_id"] = args.document
    elif args.project is not None:
        body["project_id"] = args.project
    resp = c.post("/api/queries", json=body)
    thread = resp.get("thread", {})
    emit.result(
        "Opened query #{} ({}) on project {}.".format(
            thread.get("id"), thread.get("status"), thread.get("project_id")
        ),
        {"ok": True, "thread": thread},
    )
    return 0


def query_list(args, emit) -> int:
    c = _client(args)
    params = {}
    if args.status:
        params["status"] = args.status
    if args.project is not None:
        params["project"] = args.project
    if args.document is not None:
        params["document"] = args.document
    if args.segment is not None:
        params["segment"] = args.segment
    if args.key is not None:
        params["key"] = args.key
    if args.assignee:
        params["assignee"] = args.assignee
    if args.mine:
        params["mine"] = "1"
    params["limit"] = max(1, min(args.limit, 50))
    params["offset"] = max(0, args.offset)
    body = c.get("/api/queries", params=params)
    rows = body.get("threads", [])
    total = body.get("total", len(rows))
    if emit.as_json:
        emit.json({"ok": True, "total": total, "threads": rows})
        return 0
    emit.table(
        [_q_row_line(r) for r in rows],
        headers=("id", "status", "project", "subject", "assignee", "posts", "updated"),
    )
    emit.say("%d thread(s)%s." % (total, "" if total <= len(rows) else " (showing %d)" % len(rows)))  # noqa: UP031
    return 0
