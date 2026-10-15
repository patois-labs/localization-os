"""`engine` command group: manage MT/LLM EngineProfile rows headlessly (P3, item 6).

DeepL / Google / Microsoft (NMT) + Cohere (LLM) + local self-hosted engines are
CRUD-able and testable entirely from the CLI, mirroring the `connector` group. The
business logic lives in app.services.engines (a clean service surface); this module
is a thin CLI shell over it — the web+CLI parity rule (one source of truth).

All in-process (local-root, no RBAC — same trust as editing the DB directly). Under
the multi-org sacred gate every verb is org-scoped (local_op -> require_org_scope):
`--org N` confines list/show/update/delete/test to org N and stamps `create` into it;
a foreign-org engine is an indistinguishable "no such engine" (round-3 #62). `--operator`
(or single-tenant / gate off) is unscoped — byte-identical. This mirrors the web
setup-ai engine routes' strict `prof.org_id == org` gate.

SECRETS: the API key is a password-like input (--secret api_key=...), Fernet-
encrypted, and NEVER echoed. `show`/`list` report only whether a key is set
(`key: (set)`), never the value — exactly like `connector show`.

TEST HOOK: `_set_transport()` installs a module-level httpx transport that `test`
passes into the engines.test_profile probe. None in normal use (so the CLI never
touches the network unless the operator explicitly tests an engine); the verify
suite injects an httpx.MockTransport so per-kind probes run fully offline, exactly
like tests/verify_connectors.py + cmd_connector._set_transport.

Documented in: docs/architecture/engines-and-providers.md
"""

import argparse

from ._common import (
    EXIT_USAGE,
    CliError,
    bounded_id,
    confirm,
    get_scoped_or_404,
    local_op,
    op_org_id,
)
from ._common import parse_kv as _parse_kv
from ._common import parse_secret_kv as _parse_secret_kv

# Module-level transport override for tests (see the module docstring). None in
# normal operation; the verify suite installs an httpx.MockTransport here.
_TRANSPORT = None


def _set_transport(transport) -> None:
    """Install (or clear) the module-level httpx transport used by `engine test`.
    Tests only — normal CLI runs leave this None."""
    global _TRANSPORT
    _TRANSPORT = transport


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    from app.kernel.engines import KINDS

    eng = subparsers.add_parser(
        "engine",
        help="MT/LLM engine profiles (DeepL/Google/Microsoft/Cohere/Lara/"
        "Amazon/openai_compatible/local) and quality judges (jev = hosted TypeSafe Jev, "
        "jev_local = a Jev-compatible server such as SemIf): create, list, show, update, "
        "delete, test; for judges also inspect, jev-try and import-jev-env. Lara is "
        "EXPERIMENTAL (request signing needs live verification).",
    )
    sub = eng.add_subparsers(dest="engine_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create an engine profile.")
    p.add_argument("--name", required=True, help="a unique display name for the engine.")
    p.add_argument(
        "--kind",
        required=True,
        choices=KINDS,
        help="engine kind (lara is EXPERIMENTAL -- request signing needs "
        "live verification before production use). jev / jev_local are QUALITY "
        "JUDGES: pick them only as a workflow step's --qe-provider. jev needs "
        "--secret api_key=... and always talks to https://api.typesafe.ai; jev_local "
        "needs --base-url (e.g. http://127.0.0.1:8077), the key is optional.",
    )
    p.add_argument(
        "--base-url",
        default="",
        help="override the API base URL (blank = the kind's default endpoint). Required for "
        "jev_local (e.g. http://127.0.0.1:8077 or a Tailscale address); jev is pinned to "
        "https://api.typesafe.ai and refuses any other host.",
    )
    p.add_argument(
        "--model",
        default="",
        help="model id (LLM/local engines; NMT ignores it; quality judges: blank = jev-latest).",
    )
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="K=V",
        help="a SECRET credential key=value (repeatable), e.g. "
        "--secret api_key=... (`api_key=-` reads one line from stdin, keeping the key off the "
        "command line). For a key+secret PAIR (Lara / Amazon) also "
        "pass --secret access_key_secret=... (the access-key id / AWS access "
        "key goes in api_key). Never echoed back.",
    )
    p.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="a non-secret config key=value (repeatable), e.g. "
        "--config formality=more / --config model_type=quality_optimized / "
        "--config context='a help centre' (DeepL); --config region=eastus "
        "(Microsoft) or --config region=us-east-1 (Amazon, REQUIRED); "
        "--config style=fluid / --config adapt_to=12,34 / "
        "--config glossaries=5 (Lara). For an openai_compatible host set "
        "--base-url + --model + --secret api_key=... (no extra config needed). "
        "Quality judges (jev / jev_local): --config confidence_floor=0.6 (0.5 <= value < 1.0), "
        "--config meaning_floor=0.8 (0..1), --config timeout_s=30; unset = the "
        "install defaults. Any translation engine: --config max_parallel_jobs=4 caps how many "
        "translate jobs run on it at once (1..64, only lowers the LOC_TMS_JOB_WORKERS cap; "
        "unset = no per-engine limit).",
    )
    p.add_argument(
        "--instruction",
        action="append",
        default=[],
        metavar="TEXT",
        dest="instructions",
        help="a DeepL custom instruction (repeatable, up to 10, <=300 chars each); "
        "sets config.custom_instructions as a list.",
    )
    p.add_argument(
        "--disabled",
        action="store_true",
        help="create the engine DISABLED (not offered in the pickers).",
    )
    p.set_defaults(func=_create)

    p = sub.add_parser(
        "list", help="List engine profiles (id, name, kind, model, key state, enabled)."
    )
    p.set_defaults(func=_list)

    p = sub.add_parser(
        "show", help="Show one engine profile (config + key STATE only, never the key)."
    )
    p.add_argument("engine_id", type=bounded_id, help="engine profile id.")
    p.set_defaults(func=_show)

    p = sub.add_parser(
        "update", help="Update an engine profile (config merged; a secret is set only if given)."
    )
    p.add_argument("engine_id", type=bounded_id, help="engine profile id.")
    p.add_argument("--name", default=None, help="new display name (unchanged if omitted).")
    p.add_argument(
        "--base-url",
        default=None,
        help="new base URL (unchanged if omitted). Moving it to another server (scheme, "
        "host or port) clears the stored key and secret unless --secret re-supplies them.",
    )
    p.add_argument(
        "--clear-key",
        action="store_true",
        help="Remove the stored API key; hosted Jev is disabled.",
    )
    p.add_argument(
        "--clear-access-key-secret",
        action="store_true",
        help="Remove the stored Lara/Amazon paired secret.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="new model (unchanged if omitted; blank resets a quality judge to jev-latest).",
    )
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="K=V",
        help="SECRET credential key=value to rotate (repeatable); `api_key=-` reads one "
        "line from stdin. Omit to leave the stored key untouched.",
    )
    p.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="config key=value to set/merge (repeatable). For judges, blank confidence_floor, "
        "meaning_floor or timeout_s clears to the install default. "
        "Confidence floor: 0.5 <= value < 1.0. A blank max_parallel_jobs removes the per-engine "
        "job limit.",
    )
    p.add_argument(
        "--clear-config",
        action="append",
        default=[],
        metavar="KEY",
        dest="clear_config",
        help="a config KEY to REMOVE (repeatable), e.g. --clear-config context / "
        "--clear-config custom_instructions. Removes a previously-set param; "
        "never affects the secret.",
    )
    p.add_argument(
        "--instruction",
        action="append",
        default=[],
        metavar="TEXT",
        dest="instructions",
        help="a DeepL custom instruction (repeatable, up to 10, <=300 chars each); "
        "replaces config.custom_instructions with the given list.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true", help="enable the engine.")
    g.add_argument("--disable", action="store_true", help="disable the engine.")
    p.set_defaults(func=_update)

    p = sub.add_parser("delete", help="Delete an engine profile (DESTRUCTIVE).")
    p.add_argument("engine_id", type=bounded_id, help="engine profile id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_delete)

    test_text = (
        "Test an engine's credentials/reachability with a "
        "lightweight per-kind probe (translates nothing). For a quality judge: lists its "
        "models, says whether the configured model is among them, and asks one fixed test "
        "question (hosted Jev: a few tokens, not sent once the spend cap is reached)."
    )
    p = sub.add_parser("test", help=test_text, description=test_text)
    p.add_argument("engine_id", type=bounded_id, help="engine profile id.")
    p.set_defaults(func=_test)

    models_text = (
        "List the models an engine's server offers, using the engine's stored key "
        "(the parity twin of Setup AI's Fetch models on an existing engine). Never prints the key."
    )
    p = sub.add_parser("models", help=models_text, description=models_text)
    p.add_argument("engine_id", type=bounded_id, help="engine profile id.")
    p.set_defaults(func=_models)

    # Jev Train C: the quality-judge Inspector (Setup AI Inspect page parity).
    p = sub.add_parser(
        "inspect",
        help="Show what a quality-judge engine (jev / jev_local) is asked, the exact request "
        "body, and its decision rule rendered from the real constants. Sends nothing.",
    )
    p.add_argument("engine_id", type=bounded_id, help="quality-judge engine profile id.")
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit the full inspection as JSON (same as the root --json).",
    )
    p.set_defaults(func=_inspect)

    p = sub.add_parser(
        "jev-try",
        help="Score one segment through a quality-judge engine exactly as a QE-gated step "
        "would: request, raw response, verdict, cost, and the auto-fix post-editor prompt. "
        "Hosted Jev bills a fraction of a cent per try and honours the spend cap.",
    )
    p.add_argument("engine_id", type=bounded_id, help="quality-judge engine profile id.")
    p.add_argument("--source", required=True, help="the source text.")
    p.add_argument("--target", required=True, help="the translation to judge.")
    p.add_argument("--source-locale", required=True, help="source locale, e.g. en.")
    p.add_argument("--target-locale", required=True, help="target locale, e.g. de.")
    p.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="the step's QE pass threshold, 0 to 100 (default 70).",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit the full result as JSON (same as the root --json).",
    )
    p.set_defaults(func=_jev_try)

    p = sub.add_parser(
        "import-jev-env",
        help="Create a hosted Jev engine from this install's TYPESAFE_API_KEY (the Setup AI "
        "button). Idempotent; the key is stored encrypted and never printed.",
    )
    p.set_defaults(func=_import_jev_env)


# --- helpers -----------------------------------------------------------------
def _apply_instructions(cfg: dict, instructions) -> dict:
    """Fold repeatable --instruction values into config.custom_instructions as a
    LIST (DeepL wants an array), clamped to DeepL's limits (≤10 instructions, ≤300
    chars each). Keeps CLI parity with the web textarea. Note: a plain
    --config custom_instructions=... string is ALSO accepted — engines._normalize_cfg
    coerces a scalar string into a one-element list before it is stored, so the
    per-char-iteration corruption is prevented at the config layer, not here."""
    instrs = [i.strip()[:300] for i in (instructions or []) if i.strip()][:10]
    if instrs:
        cfg["custom_instructions"] = instrs
    return cfg


def _apply_paired_secret(cfg: dict, secret: dict) -> dict:
    """Route a --secret access_key_secret=... (the paired secret for Lara/Amazon key+
    secret auth) into the config as PLAINTEXT `access_key_secret`; the service layer
    (engines._normalize_cfg) Fernet-encrypts it into access_key_secret_enc at store
    time and drops the plaintext, so it is never persisted or echoed in clear. The
    primary api_key stays on the dedicated api_key_enc column. Absent -> cfg unchanged
    (so an update without the secret keeps the stored one)."""
    aks = secret.get("access_key_secret")
    if aks:
        cfg = {**cfg, "access_key_secret": aks}
    return cfg


def _get_profile(pid: int, org_id=None):
    """Load an engine profile, enforcing the in-process --org scope. A profile in
    ANOTHER org (or a global engine while scoped, or a missing id) is an
    indistinguishable 'no such engine' (exit 2) — mirroring the web setup-ai routes,
    which strict-equality gate `prof.org_id == org` for show/update/delete/test.
    org_id None (single-tenant / --operator / gate off) is unscoped — byte-identical."""
    from app.kernel import engines

    return get_scoped_or_404(engines.get_profile(pid), pid, "engine", org_id)


# --- create ------------------------------------------------------------------
def _create(args, emit) -> int:
    # #62: the engine family used only require_local (no org gate). local_op adds the
    # SaaS in-process operator gate (require_org_scope) so a multi-org caller must be
    # scoped; a new engine is STAMPED into the scoped org (op_org_id), matching the
    # web setup-ai create (org_id=_my_org_id). None scope = global/shared engine.
    local_op(args, emit)
    from app.kernel import audit, engines

    secret = _parse_secret_kv(args.secret, "--secret")
    cfg = _apply_instructions(
        _parse_kv(args.config, "--config"), getattr(args, "instructions", None)
    )
    cfg = _apply_paired_secret(cfg, secret)
    api_key = secret.get("api_key", "")
    actor = getattr(args, "actor", None) or "cli"
    try:
        prof = engines.create_profile(
            args.name,
            args.kind,
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            cfg=cfg,
            enabled=not args.disabled,
            actor=actor,
            org_id=op_org_id(args),
        )
    except ValueError as e:
        raise CliError(str(e), EXIT_USAGE) from e
    audit.log(
        actor=actor,
        action="engine.create",
        target=f"engine:{prof.id}",
        detail=f"kind={prof.kind} name={prof.name}",
    )
    row = engines.engine_row(prof)
    # EXPERIMENTAL visibility (review finding): the web Setup AI page has always
    # shown this; the CLI previously showed nothing at all, so an operator scripting
    # engine creation had zero signal that Lara's auth scheme needs live
    # verification. engines.EXPERIMENTAL_KINDS is the single source of truth (also
    # read by the web banner) — text mode gets an explicit warning line (stderr,
    # via emit.warn, so it never pollutes --json stdout); JSON gets the row's
    # "experimental" field (additive — existing keys are unchanged).
    if row.get("experimental"):
        emit.warn(
            "EXPERIMENTAL: Lara request signing needs live verification before production use."
        )
    # Quality-judge advice (e.g. a networked jev_local with no key): stderr in text mode,
    # an additive `warnings` list in JSON -- the same text the web banner and /api show.
    warnings = engines.profile_warnings(prof)
    for warning in warnings:
        emit.warn(warning)
    emit.result(
        f"Created {prof.kind} engine #{prof.id}: {prof.name} "
        f"(key: {'set' if prof.api_key_enc else 'none'}).",
        {"ok": True, **row, "warnings": warnings},
    )
    return 0


# --- list --------------------------------------------------------------------
def _list(args, emit) -> int:
    # #62: `engine list` was fully ungated (showed every org's engines, incl. their
    # base_url/model/key-state). Gate + confine to the scoped org (op_org_id); None
    # (single-tenant / --operator) lists all, matching web list_profiles(org_id=).
    local_op(args, emit)
    from app.kernel import engines

    profs = engines.list_profiles(org_id=op_org_id(args))
    # Text-mode "kind" column gets an "(experimental)" suffix (engines.EXPERIMENTAL_KINDS
    # is the source of truth); JSON keeps `kind` untouched and carries the boolean
    # "experimental" field instead (engine_row), so machine consumers are unaffected.
    rows = [
        (
            p.id,
            p.name,
            p.kind + (" (experimental)" if p.kind in engines.EXPERIMENTAL_KINDS else ""),
            p.model or "",
            "set" if p.api_key_enc else "",
            "yes" if p.enabled else "",
        )
        for p in profs
    ]
    if emit.as_json:
        emit.json({"ok": True, "engines": [engines.engine_row(p) for p in profs]})
    else:
        emit.table(rows, headers=("id", "name", "kind", "model", "key", "enabled"))
    return 0


# --- show --------------------------------------------------------------------
def _show(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import engines

    prof = _get_profile(args.engine_id, op_org_id(args))
    info = engines.engine_row(prof)
    info["created_by"] = prof.created_by
    if emit.as_json:
        emit.json({"ok": True, **info})
    else:
        kind_suffix = " (experimental)" if info.get("experimental") else ""
        emit.say(f"Engine #{prof.id}: {prof.name} (kind: {prof.kind}{kind_suffix})")
        emit.say(
            f"  base_url: {engines.display_base_url(prof.base_url) or '(default)'}   "
            f"model: {prof.model or '(none)'}"
        )
        emit.say(
            f"  key: {'(set)' if prof.api_key_enc else '(none)'}   "
            f"enabled: {'yes' if prof.enabled else 'no'}   "
            f"provider name: engine:{prof.id}"
        )
        # Use the REDACTED config (engine_row) so a paired secret's ciphertext never
        # prints — it shows as a masked <name>_set flag, matching the JSON path.
        cfg = info.get("config") or {}
        emit.say("  config:")
        emit.table(
            sorted((k, str(v)) for k, v in cfg.items()) or [("(none)", "")],
            headers=("key", "value"),
        )
    return 0


# --- update ------------------------------------------------------------------
def _update(args, emit) -> int:
    # #62 HEADLINE: cross-org `engine update --base-url` was a live content-exfil
    # primitive (repoint a co-tenant's endpoint). Gate + scope-resolve so a foreign
    # engine is 404 BEFORE any write.
    local_op(args, emit)
    from app.kernel import audit, engines

    before = _get_profile(args.engine_id, op_org_id(args))
    secret = _parse_secret_kv(args.secret, "--secret")
    cfg = _apply_instructions(
        _parse_kv(args.config, "--config"), getattr(args, "instructions", None)
    )
    cfg = _apply_paired_secret(cfg, secret)
    clear = [k.strip() for k in getattr(args, "clear_config", []) if k.strip()]
    enabled = True if args.enable else (False if args.disable else None)
    try:
        prof = engines.update_profile(
            args.engine_id,
            name=args.name,
            base_url=args.base_url,
            model=args.model,
            api_key=secret.get("api_key") or None,
            cfg=cfg or None,
            clear=clear or None,
            enabled=enabled,
            clear_api_key=args.clear_key,
            clear_access_key_secret=args.clear_access_key_secret,
            actor=getattr(args, "actor", None) or "cli",
        )
    except ValueError as e:
        raise CliError(str(e), EXIT_USAGE) from e
    actor = getattr(args, "actor", None) or "cli"
    audit.log(
        actor=actor,
        action="engine.update",
        target=f"engine:{prof.id}",
        detail=f"config keys: {', '.join(sorted(cfg)) or 'none'}"
        + (f"; cleared: {', '.join(sorted(clear))}" if clear else "")
        + ("; secret changed" if secret.get("api_key") else "")
        + ("; API key removed" if args.clear_key else "")
        + ("; access key secret removed" if args.clear_access_key_secret else ""),
    )
    warnings = engines.profile_warnings(prof)
    # #218: moving the destination drops the stored credentials; tell the operator.
    if engines.credentials_dropped(
        before,
        prof,
        clear_key=bool(args.clear_key),
        clear_secret=bool(args.clear_access_key_secret),
    ):
        warnings = [engines.DESTINATION_CLEARED_NOTE, *warnings]
    for warning in warnings:
        emit.warn(warning)
    emit.result(
        f"Updated engine #{prof.id}: {prof.name}.",
        {"ok": True, **engines.engine_row(prof), "warnings": warnings},
    )
    return 0


# --- delete ------------------------------------------------------------------
def _delete(args, emit) -> int:
    # #62: cross-org DESTRUCTION of another org's engine profile. Gate + scope-resolve
    # BEFORE the confirm() prompt (no name/kind oracle) so a foreign engine is 404.
    local_op(args, emit)
    from app.kernel import audit, engines

    prof = _get_profile(args.engine_id, op_org_id(args))
    if not confirm(emit, f"Delete {prof.kind} engine #{prof.id} ({prof.name})?", args.yes):
        raise CliError("aborted.", 1)
    try:
        engines.delete_profile(prof.id)  # FAILS CLOSED while referenced as engine:<id>
    except engines.EngineInUseError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    actor = getattr(args, "actor", None) or "cli"
    audit.log(
        actor=actor, action="engine.delete", target=f"engine:{prof.id}", detail=f"kind={prof.kind}"
    )
    emit.result(
        f"Deleted engine #{prof.id} ({prof.name}).", {"ok": True, "id": prof.id, "deleted": True}
    )
    return 0


# --- test --------------------------------------------------------------------
def _test(args, emit) -> int:
    # #62: `engine test` on a foreign engine confirmed its existence + probed its
    # endpoint/credentials cross-org. Gate + scope-resolve so a foreign engine is 404.
    local_op(args, emit)
    from app.kernel import engines

    _get_profile(args.engine_id, op_org_id(args))
    ok, msg = engines.test_profile(args.engine_id, transport=_TRANSPORT)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {msg}",
        {"ok": ok, "engine_id": args.engine_id, "message": msg},
    )
    return 0 if ok else 1


# --- models ------------------------------------------------------------------
def _models(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import engines

    _get_profile(args.engine_id, op_org_id(args))
    res = engines.list_models(args.engine_id, transport=_TRANSPORT) or {}
    models = res.get("models") or []
    if models:
        text = "\n".join(models)
    elif res.get("auth_failed"):
        text = "FAILED: the server refused the key."
    elif res.get("unsupported"):
        text = "FAILED: this engine kind has no model list."
    elif res.get("blocked"):
        text = "FAILED: the server address is refused by the local-engine egress policy."
    elif res.get("unreachable"):
        text = "FAILED: the server did not answer. Check the base URL and that it is running."
    else:
        text = "No models returned."
    emit.result(
        text,
        {
            "ok": bool(models),
            "engine_id": args.engine_id,
            "models": models,
            "blocked": bool(res.get("blocked")),
            "auth_failed": bool(res.get("auth_failed")),
            "unreachable": bool(res.get("unreachable")),
        },
    )
    return 0 if models else 1


# --- quality-judge Inspector (Jev Train C) ------------------------------------
def _get_judge(args):
    """The scoped profile, refused unless it is a quality judge. A foreign or missing
    id is the usual 'no such engine' (exit 2), checked first so nothing is revealed."""
    from app.kernel import jev_inspector

    prof = _get_profile(args.engine_id, op_org_id(args))
    if not jev_inspector.is_judge(prof):
        raise CliError(
            f"engine #{prof.id} is not a quality judge (kind {prof.kind}); inspect and "
            "jev-try work on jev and jev_local engines only.",
            EXIT_USAGE,
        )
    return prof


def _inspect(args, emit) -> int:
    local_op(args, emit)
    import json

    from app.kernel import jev_inspector

    data = jev_inspector.inspect_payload(_get_judge(args))
    if emit.as_json:
        emit.json(data)
        return 0
    e = data["engine"]
    emit.say(f"Engine #{e['id']}: {e['name']} ({e['kind_label']})")
    emit.say(f"  model: {data['model']}   endpoint: {data['endpoint']}")
    if not data["live"]:
        emit.say(f"  NOT LIVE: {data['not_live_reason']}")
    for warning in data.get("warnings", []):
        emit.warn(warning)
    emit.say(f"  {data['cost_note']}")
    emit.say("")
    emit.say("What Jev is asked:")
    emit.table(
        [
            (q["id"], q["type"], lv["level"], " ".join(lv["signals"]))
            for q in data["criteria"]
            for lv in q["levels"]
        ],
        headers=("question", "type", "level", "meaning"),
    )
    emit.say("")
    emit.say("How it decides:")
    emit.say(data["rule"]["text"])
    emit.say("")
    emit.say("Request body template (the key rides only in an Authorization header):")
    emit.say(json.dumps(data["request_template"], indent=2))
    emit.say("")
    emit.say(f"Ledger for this engine: {data['ledger_url']}")
    return 0


def _jev_try(args, emit) -> int:
    local_op(args, emit)
    import json

    from app.kernel import jev_inspector

    prof = _get_judge(args)
    try:
        out = jev_inspector.try_it(
            prof,
            args.source,
            args.target,
            args.source_locale,
            args.target_locale,
            args.threshold,
            caller="cli:" + (getattr(args, "actor", None) or "cli"),
            org_id=op_org_id(args),
        )
    except jev_inspector.TryBusy as exc:
        raise CliError(f"{exc} (retry in {exc.retry_after} s)", 1) from exc
    except jev_inspector.InspectError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    if emit.as_json:
        emit.json(out)
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        emit.say(f"NOT SENT: {out.get('message')}")
        return 1
    card = out["card"]
    if card["skipped"]:
        head = f"verdict: {out['verdict']} ({card['reason']}"
        head += f": {card['error']})" if card.get("error") else ")"
    else:
        head = f"verdict: {out['verdict']} (score {card['score']}, threshold {out['threshold']})"
    emit.say(head)
    if out.get("message"):
        emit.say(out["message"])
    if card["unsure"]:
        emit.say("unsure: " + ", ".join(card["unsure"]))
    if card["levels"]:
        emit.table(sorted(card["levels"].items()), headers=("dimension", "level"))
    cost = f"USD {out['cost_usd']:.6f}" if out["priced"] else "$0 (local)"
    emit.say(f"cost: {cost}   judge latency: {out['latency_ms']} ms   HTTP {out['status']}")
    emit.say("")
    emit.say("Request sent:")
    emit.say(json.dumps(out["request"], indent=2))
    emit.say("Raw response:")
    emit.say(json.dumps(out["response"], indent=2))
    fix = out["autofix"]
    emit.say("")
    emit.say("Auto-fix post-editor, round 1: " + fix["why"])
    emit.say("System prompt: " + fix["system"])
    if fix["prompt"]:
        emit.say("Prompt:")
        emit.say(fix["prompt"])
    emit.say("")
    emit.say(f"Ledger for this engine: {out['ledger_url']}")
    return 0


def _import_jev_env(args, emit) -> int:
    # Creates a NEW engine stamped into the scoped org (like `engine create`); no
    # existing foreign target is read or written.
    local_op(args, emit)
    from app.kernel import engines, jev_inspector

    actor = getattr(args, "actor", None) or "cli"
    try:
        prof, created = jev_inspector.import_env(actor=actor, org_id=op_org_id(args))
    except jev_inspector.InspectError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(
        (
            f"Created jev engine #{prof.id}: {prof.name} (key: set)."
            if created
            else f"A hosted Jev engine already exists (#{prof.id}: {prof.name}); nothing imported."
        ),
        {"ok": True, "created": created, **engines.engine_row(prof)},
    )
    return 0
