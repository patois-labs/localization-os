"""`providers` command group: inspect translation providers + manage cloud keys.

Subcommands:
  * `models` — list the model ids a provider REFERENCE can serve (the CLI parity
    surface for the web workflow-builder model dropdowns); goes through the SAME
    service seam (providers.list_models_for_ref).
  * `key set|show|clear` — manage the in-app cloud API keys (Claude/Gemini/OpenAI),
    the CLI parity surface for the Setup AI cloud tiles. Both surfaces call the SAME
    service seam (app.services.cloud_keys), so a key stored via either takes effect
    everywhere and precedence (stored overrides .env) is identical.

In-process only (local-root, no RBAC — same trust as editing the DB directly),
mirroring the `engine` group. Org-scoped when multi-org is active (--org / --operator),
so an engine:<id> ref only lists when it belongs to the scoped org. Cloud keys are
install-global config (like `config set`), so the `key` commands use require_local
only — no org scope.

SECRETS: a cloud key is NEVER echoed. `key show` reports source + a last4
fingerprint only. `key set` reads the value from --key, or from STDIN when --key is
omitted (so the key never lands in shell history).

Documented in: docs/architecture/engines-and-providers.md
"""

from ._common import EXIT_FAIL, EXIT_USAGE, op_org_id, require_local, require_org_scope


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    prov = subparsers.add_parser(
        "providers",
        help="Inspect translation providers (list the models a provider ref can serve).",
    )
    sub = prov.add_subparsers(dest="providers_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "models",
        help="List the model ids a provider ref can serve "
        "(claude/gemini/openai/local/engine:<id>). One id per line; a non-listable "
        "ref (no key/base URL, a plain-NMT engine kind, or an unknown ref) prints a "
        "clean message and exits 1. This is the CLI parity surface for the web "
        "workflow-builder model dropdowns.",
    )
    p.add_argument(
        "--provider",
        required=True,
        metavar="REF",
        help="provider reference: claude, gemini, openai, local, or engine:<id>.",
    )
    p.set_defaults(func=_models)

    # --- cloud key management (Setup AI cloud-tile parity) --------------------
    from app.kernel.cloud_keys import CLOUD_PROVIDERS

    key = sub.add_parser(
        "key",
        help="Manage in-app cloud API keys (claude/gemini/openai): set, show, "
        "clear. A stored key is encrypted at rest and overrides the .env key; the key "
        "value is never echoed.",
    )
    ksub = key.add_subparsers(dest="providers_key_cmd", metavar="<subcommand>")
    ksub.required = True

    ks = ksub.add_parser(
        "set",
        help="Set/replace a cloud provider's API key (encrypted; overrides "
        ".env immediately). Reads the key from --key, or from STDIN when --key is "
        "omitted (keeps it out of shell history).",
    )
    ks.add_argument(
        "provider", choices=CLOUD_PROVIDERS, help="cloud provider: claude, gemini, or openai."
    )
    ks.add_argument(
        "--key",
        default=None,
        metavar="VALUE",
        help="the API key. Omit to read one line from STDIN. Surrounding "
        "whitespace/quotes are trimmed. Never echoed back.",
    )
    ks.set_defaults(func=_key_set)

    ksh = ksub.add_parser(
        "show",
        help="Show every cloud provider's key SOURCE (app/env/none) and a "
        "last4 fingerprint. Never prints the key value.",
    )
    ksh.set_defaults(func=_key_show)

    kc = ksub.add_parser(
        "clear",
        help="Remove a provider's app-stored key (falls back to the .env "
        "key if present, else keyless).",
    )
    kc.add_argument(
        "provider", choices=CLOUD_PROVIDERS, help="cloud provider: claude, gemini, or openai."
    )
    kc.set_defaults(func=_key_clear)


# --- models ------------------------------------------------------------------
def _models(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    from app.kernel.providers import list_models_for_ref

    ref = args.provider
    models = list_models_for_ref(ref, op_org_id(args))
    if not models:
        # Non-zero exit + a clean, ASCII-only message so a script can branch on it.
        if emit.as_json:
            emit.json(
                {
                    "ok": False,
                    "provider": ref,
                    "models": [],
                    "error": "no models available for this provider ref",
                }
            )
        else:
            emit.warn(
                f"no models available for '{ref}' (no key/base URL, a non-listable "
                "engine kind, or an unknown ref)."
            )
        return EXIT_FAIL
    if emit.as_json:
        emit.json({"ok": True, "provider": ref, "models": models})
    else:
        for m in models:
            emit.say(m)  # one id per line, ASCII-safe via the Emitter
    return 0


# --- key set -----------------------------------------------------------------
def _key_set(args, emit) -> int:
    require_local(args)
    import sys

    from app.kernel import cloud_keys

    from ._common import CliError

    # --key wins; otherwise read one line from STDIN (keeps the key out of shell
    # history). A missing --key with an interactive TTY and no piped input would
    # block on input(), so require a value one way or the other.
    raw = args.key
    if raw is None:
        if sys.stdin is None or sys.stdin.isatty():
            raise CliError(
                "no key provided: pass --key VALUE or pipe the key on STDIN "
                "(e.g. `... key set claude < keyfile`).",
                EXIT_USAGE,
            )
        raw = sys.stdin.readline()
    try:
        tail = cloud_keys.set_key(args.provider, raw, actor="cli")
    except ValueError as e:
        raise CliError(str(e), EXIT_USAGE) from e
    # Never echo the key — only the last4 fingerprint.
    emit.result(
        f"{args.provider}: key set (source: app, ....{tail}).",
        {"ok": True, "provider": args.provider, "source": "app", "last4": tail},
    )
    return 0


# --- key show ----------------------------------------------------------------
def _key_show(args, emit) -> int:
    require_local(args)
    from app.kernel import cloud_keys

    rows = []
    payload = {}
    for p in cloud_keys.CLOUD_PROVIDERS:
        st = cloud_keys.status(p)
        fp = ("...." + st["last4"]) if st["last4"] else "(none)"
        rows.append((p, st["source"], fp))
        payload[p] = {
            "source": st["source"],
            "last4": st["last4"],
            "stored": st["stored"],
            "has_env": st["has_env"],
        }
    if emit.as_json:
        emit.json({"ok": True, "keys": payload})
    else:
        emit.table(rows, headers=("provider", "source", "fingerprint"))
    return 0


# --- key clear ---------------------------------------------------------------
def _key_clear(args, emit) -> int:
    require_local(args)
    from app.kernel import cloud_keys

    had = cloud_keys.remove_key(args.provider, actor="cli")
    src = cloud_keys.source(args.provider)  # "env" (fell back) or "none"
    if had:
        msg = f"{args.provider}: app key removed (now source: {src})."
    else:
        msg = f"{args.provider}: no app key was set (source: {src})."
    emit.result(msg, {"ok": True, "provider": args.provider, "removed": had, "source": src})
    return 0
