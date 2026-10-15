"""workflow prompt system* with --workflow: per-workflow overrides of the two editable system
prompt heads (GitHub #356 phase 2b). The in-process arm and the --remote arm print through the
SAME emitters from the same `system_prompts.workflow_payload` / `workflow_editable_detail`
bodies, so they cannot drift.

  workflow prompt system --workflow N [--key K]    both heads as they apply to workflow N
  workflow prompt system-set --workflow N --key K (--body T | --file F)
  workflow prompt system-reset --workflow N --key K     inherit again (history kept)
  workflow prompt system-history --workflow N --key K
  workflow prompt system-restore --workflow N --key K --version V

Resolution at run time: the workflow's own text > the org's text > the built-in text.

WHO MAY EDIT: over --remote the server requires an admin or a manager (#630; the workflow in
the caller's org and customer ring, a foreign workflow is a 404; reads are admin, manager or
linguist); the
in-process CLI is the trusted operator like the other authoring verbs, org-scoped by --org on
a multi-org install. Refused while the system_prompt_overrides feature is off.

Documented in: docs/architecture/workflow-engine.md
"""

from urllib.parse import quote

from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    get_scoped_or_404,
    op_org_id,
    remote_mode,
    require_org_scope,
)
from app.cli._remote_impl_common import _client
from app.cli._remote_impl_part6 import (
    _emit_system_prompt_history,
    _emit_system_prompt_saved,
)


def _url(args, tail: str = "") -> str:
    key = quote(str(args.key), safe="")
    return f"/api/workflows/{int(args.workflow)}/system-prompts/{key}{tail}"


def _check_key(system_prompts, args) -> None:
    if args.key not in system_prompts.EDITABLE_KEYS:
        raise CliError(f"{args.key!r} is not an editable system prompt.", EXIT_USAGE)


def _write_gate(system_prompts) -> None:
    if not system_prompts.overrides_enabled():
        raise CliError(system_prompts.OFF_MESSAGE, EXIT_FAIL)


def _workflow(session, args):
    from app.models import Workflow

    return get_scoped_or_404(
        session.get(Workflow, args.workflow), args.workflow, "workflow", op_org_id(args)
    )


def _emit_heads(emit, payload, key=None) -> int:
    """Print the heads of one workflow: a table, or with `key` one head in full."""
    editable = payload.get("editable", [])
    if key:
        hit = [e for e in editable if e["key"] == key]
        if not hit:
            raise CliError(f"no editable system prompt with key {key!r}.", EXIT_USAGE)
        e = hit[0]
        lines = [
            f"{e['name']} (workflow #{e['workflow_id']})",
            e["used_in"],
            "",
            "Always added before the text (locked):",
            e["floor_before"],
            "",
            f"Inherited text ({e['inherited_source']}):",
            e["inherited_body"],
        ]
        if e.get("override"):
            lines += [
                "",
                f"This workflow's own text (version {e['override']['version']}):",
                e["override"]["body"],
            ]
        lines += ["", "Always added after the text (locked):", e["floor_after"]]
        lines += ["", f"In force now: {e['effective_source']}."]
        if not payload.get("overrides_enabled", False):
            lines += ["", "Overrides are switched off (setting system_prompt_overrides_enabled=0)."]
        emit.result(
            "\n".join(lines),
            {"ok": True, "overrides_enabled": payload.get("overrides_enabled"), "editable": [e]},
        )
        return 0
    if emit.as_json:
        emit.json({"ok": True, **payload})
        return 0
    emit.say(f"System prompt heads of workflow #{payload.get('workflow_id')}:")
    emit.table(
        [
            (
                e["key"],
                e["name"],
                f"own v{e['override']['version']}" if e.get("override") else "inherits",
                e["effective_source"],
            )
            for e in editable
        ],
        headers=("key", "name", "this workflow", "in force"),
    )
    if not payload.get("overrides_enabled", False):
        emit.say("")
        emit.say("Overrides are switched off (setting system_prompt_overrides_enabled=0).")
    return 0


def system_show(args, emit) -> int:
    """`workflow prompt system --workflow N [--key K]`."""
    if remote_mode(args):
        body = _client(args).get(f"/api/workflows/{int(args.workflow)}/system-prompts")
        return _emit_heads(emit, body, getattr(args, "key", None))
    require_org_scope(args, emit)
    from app.kernel import system_prompts

    with _session() as session:
        _workflow(session, args)
        payload = system_prompts.workflow_payload(session, op_org_id(args), args.workflow)
    return _emit_heads(emit, payload, getattr(args, "key", None))


def system_set(args, emit, text: str) -> int:
    """`workflow prompt system-set --workflow N --key K ...` (`text` already read)."""
    if remote_mode(args):
        out = _client(args).put(_url(args), json={"body": text})
        return _emit_system_prompt_saved(args, emit, out, "Saved")
    require_org_scope(args, emit)
    from app.kernel import system_prompts

    _write_gate(system_prompts)
    _check_key(system_prompts, args)
    with _session() as session:
        _workflow(session, args)
        try:
            out = system_prompts.save_workflow_override(
                session,
                op_org_id(args),
                args.workflow,
                args.key,
                text,
                getattr(args, "actor", None) or "cli",
            )
        except system_prompts.OverrideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    return _emit_system_prompt_saved(args, emit, out, "Saved")


def system_reset(args, emit) -> int:
    """`workflow prompt system-reset --workflow N --key K`: inherit again."""
    if remote_mode(args):
        out = _client(args).delete(_url(args))
        return _emit_system_prompt_saved(args, emit, out, "Reset")
    require_org_scope(args, emit)
    from app.kernel import system_prompts

    _write_gate(system_prompts)
    _check_key(system_prompts, args)
    with _session() as session:
        _workflow(session, args)
        try:
            out = system_prompts.reset_workflow_override(
                session,
                op_org_id(args),
                args.workflow,
                args.key,
                getattr(args, "actor", None) or "cli",
            )
        except system_prompts.OverrideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    return _emit_system_prompt_saved(args, emit, out, "Reset")


def system_restore(args, emit) -> int:
    """`workflow prompt system-restore --workflow N --key K --version V`."""
    if remote_mode(args):
        out = _client(args).post(_url(args, "/restore"), json={"version": int(args.version)})
        return _emit_system_prompt_saved(args, emit, out, "Restored")
    require_org_scope(args, emit)
    from app.kernel import system_prompts

    _write_gate(system_prompts)
    _check_key(system_prompts, args)
    with _session() as session:
        _workflow(session, args)
        try:
            out = system_prompts.restore_workflow_override(
                session,
                op_org_id(args),
                args.workflow,
                args.key,
                args.version,
                getattr(args, "actor", None) or "cli",
            )
        except system_prompts.OverrideError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    return _emit_system_prompt_saved(args, emit, out, "Restored")


def system_history(args, emit) -> int:
    """`workflow prompt system-history --workflow N --key K`: newest first (flag-independent)."""
    if remote_mode(args):
        body = _client(args).get(_url(args))
        return _emit_system_prompt_history(args, emit, body["prompt"])
    require_org_scope(args, emit)
    from app.kernel import system_prompts

    _check_key(system_prompts, args)
    with _session() as session:
        _workflow(session, args)
        prompt = system_prompts.workflow_editable_detail(
            session, op_org_id(args), args.workflow, args.key
        )
    return _emit_system_prompt_history(args, emit, prompt)
