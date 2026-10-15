"""`config get|set|list|apply|export-env` - thin, cache-coherent wrappers over
app.services.settings (the exact same store the web UI writes, so there is one
source of truth). Secrets are never echoed: get/list on a secret key show
"(set)"/"(unset)".

Unlocks the keys that have a store but no UI form today: mt_concurrency,
sync_scheduler_enabled, api_rate_limit_per_minute, project_scoping. The session limits
(session_idle_minutes, and session_max_hours, the absolute limit from sign-in, a11y T9)
are ALSO on the web Security page's Sessions card: same store, same validation. The
password policy for local accounts (password_min_length, password_require_classes,
password_block_common, password_history, password_max_age_days) is likewise on the
Security page's Passwords card, and who must use two-step sign-in (mfa_required: off, admins
or all; a sensitive key, so `config set` asks to confirm) on its Two-step sign-in card.

`config show` reads the configured private-egress policy locally or over --remote.
`config features` and `config feature enable|disable <slug>` also work remotely.
Other verbs remain host-local. Host-only settings cannot be changed over the wire.

Documented in: docs/architecture/configuration.md
"""

import tomllib

from app import features as _features_registry

from . import _remote_impl
from ._common import EXIT_USAGE, CliError, confirm, is_secret_key, remote_mode, require_local


# Setting keys backed by encrypted-at-rest storage (settings.get_secret/set_secret), NOT the
# plaintext Setting value. `config set` routes these through set_secret so they are Fernet-encrypted
# at rest on EVERY write surface (not just /api).
#
# app.services.settings.SECRET_SETTING_KEYS is the ONE source of truth. This module DERIVES from it
# rather than keeping a second literal -- the two had DIVERGED on main (the service copy carried
# `saml_sp_private_key_enc`, the CLI copy did not) despite the "MUST stay in sync" contract, which
# would let a CLI `config set saml_sp_private_key_enc` store the SP private key as PLAINTEXT while
# /api encrypts it. Resolved lazily (kernel facade) so CLI registration stays free of an
# app.services/app.db import, and exposed as `_ENCRYPTED_SECRET_KEYS` via __getattr__ for callers.
def _encrypted_secret_keys() -> frozenset:
    """Fernet-at-rest secret keys, from the one source of truth (settings.SECRET_SETTING_KEYS)."""
    from app.kernel import settings

    return settings.SECRET_SETTING_KEYS


# Security-sensitive keys: flipping these changes the trust posture of the whole
# install, so `config set` confirms before writing (unless --yes).
#
# app.services.settings.SENSITIVE_KEYS is the ONE source of truth (the R3 /api config route
# reads the SAME frozenset). This module DERIVES from it rather than keeping a second literal --
# the two had DIVERGED on main (the service copy carried data_export_allow_plaintext_staging, the
# CLI copy did not) despite the "MUST stay in sync" contract, which would let the CLI skip a
# confirm the /api route enforces. It is imported LAZILY through the kernel facade (mirroring
# _common's settings reach) so CLI registration stays free of an app.services/app.db import, and
# resolved as the module attribute `_SENSITIVE_KEYS` via __getattr__ so existing callers/tests that
# read `cmd_config._SENSITIVE_KEYS` keep working. verify_cli pins the identity so they can never
# diverge again.
def _sensitive_keys() -> frozenset:
    """The security-sensitive key set, from the single source of truth (settings.SENSITIVE_KEYS)."""
    from app.kernel import settings

    return settings.SENSITIVE_KEYS


def __getattr__(name: str):
    # PEP 562: resolve these lazily so we neither keep a divergent literal nor pull app.services
    # into CLI registration. Function-body references use the accessor helpers directly.
    if name == "_SENSITIVE_KEYS":
        return _sensitive_keys()
    if name == "_ENCRYPTED_SECRET_KEYS":
        return _encrypted_secret_keys()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Rough grouping for `config list` (human readability only). Any key not listed
# falls into "other".
_GROUPS = {
    "capacity": [
        "LOC_TMS_MAX_CONCURRENT_RUNS",
        "LOC_TMS_DB_POOL_SIZE",
        "LOC_TMS_DB_MAX_OVERFLOW",
        "LOC_TMS_SQLITE_CACHE_KB",
        "LOC_TMS_ANYIO_WORKERS",
        "LOC_TMS_STREAM_WORKERS",
        "LOC_TMS_STREAM_WAIT_S",
        "LOC_TMS_RUN_WORKERS",
        "LOC_TMS_JOB_WORKERS",
        "LOC_TMS_MEMORY_BUDGET_MB",
    ],
    "general": [
        "brand_name",
        "brand_llmemory",
        "brand_ledger",
        "org_name",
        "accessibility_contact",
        "default_source_locale",
        "default_ui_locale",
    ],
    "providers": [
        "default_provider",
        "default_fallback_provider",
        "model_claude",
        "model_gemini",
        "model_openai",
        "local_base_url",
        "local_api_key",
        "local_model",
        # #342: pinned sampling for every local-engine call (temperature always,
        # seed only when non-blank); judge tasks read the separate judge temp.
        "local_temperature",
        "local_judge_temperature",
        "local_seed",
        "local_model_warmup",
        "mt_concurrency",
    ],
    "spend": ["spend_cap_usd", "spend_period"],
    "security": [
        "security_auth_required",
        "egress_private_allowlist",
        "security_allowed_hosts",
        "trusted_proxy_ips",
        "login_trusted_networks",
        "session_idle_minutes",
        "session_max_hours",
        "password_min_length",
        "password_require_classes",
        "password_block_common",
        "password_history",
        "password_max_age_days",
        "mfa_required",
        "apikey_max_age_days",
        "project_scoping",
        "audit_enabled",
        "audit_fail_open",
        "pii_redaction",
        "pii_ner_enabled",
        "pii_ner_spacy_model",
        "pii_ner_entities",
        "encrypt_uploads",
        "encrypt_db",
        "api_rate_limit_per_minute",
        "api_read_rate_limit_per_minute",
    ],
    "identity": [
        "sso_enabled",
        "sso_provider",
        "sso_issuer",
        "sso_client_id",
        "sso_client_secret",
        "sso_default_role",
        "sso_trust_unverified_email",
        "ldap_enabled",
        "ldap_host",
        "ldap_ca_cert",
        "ldap_base_dn",
        "ldap_bind_dn",
        "ldap_bind_password_enc",
        "ldap_user_filter",
        "ldap_default_role",
    ],
    "connectors": [
        "sync_scheduler_enabled",
        "project_automation_enabled",
        "allow_mock_egress",
        "oauth_airtable_client_id",
        "oauth_airtable_client_secret",
        "oauth_atlassian_client_id",
        "oauth_atlassian_client_secret",
    ],
    "smtp": [
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "smtp_tls",
        "notify_on_review_assigned",
        "notify_on_budget_threshold",
        "notify_outbound_enabled",
        "app_base_url",
    ],
    "segmentation": ["srx_enabled", "srx_file"],
    "tm": ["tm_lock_ice", "tm_hundreds_enter_lqa", "tm_context_floor"],
    "retention": ["retention_enabled", "retention_days", "notify_on_job_finish"],
    "vendor_studio": ["vendor_studio_url", "vendor_studio_sso_secret"],
    # DERIVED from the central feature-flag registry (app/features.py imports nothing,
    # so this is a safe import at registration) -- so a new toggleable feature's key
    # is grouped here automatically, with no second hand-maintained list to drift.
    "features": [f.setting_key for f in _features_registry.FEATURES],
}

# Env vars a fresh install may want to set (the B1 install-time knobs). Rendered
# by `config export-env`. Secrets are placeholders.
_ENV_SCAFFOLD = [
    ("HOST", "127.0.0.1", False),
    ("PORT", "8000", False),
    # MED-3: NOT secret-flagged. _export_env never substitutes a live value for this key
    # (only HOST/PORT/DEFAULT_PROVIDER/LOG_FORMAT are), so it always renders its blank
    # example; secret-flagging it would emit "DATABASE_URL=<secret>" and break the
    # zero-config SQLite default (a blank here means "fall back to SQLite").
    ("DATABASE_URL", "", False),  # blank = SQLite default
    ("LOC_TMS_DATA_DIR", "", False),  # blank = <repo>/data
    ("LOC_TMS_ANYIO_WORKERS", "16", False),
    ("LOC_TMS_RUN_WORKERS", "4", False),
    ("LOC_TMS_JOB_WORKERS", "2", False),
    ("LOC_TMS_MEMORY_BUDGET_MB", "", False),
    ("LOC_TMS_SERVER", "", False),  # "1" for a real server install
    ("LOC_TMS_EMBEDDED", "", False),  # "1" for an API-only headless install (implies SERVER)
    # Embedded trust-host-auth (Train 0b): tokenless /api when the host boundary already
    # authenticates. Active only when embedded AND the `embedded_trust_host_auth` Setting
    # is "1" (host-only). PRINCIPAL is the required actor email; ROLE defaults to viewer
    # (admin requires "admin" in SCOPES); ORG_ID is required only under multi_org_enabled.
    # HEADER_SECRET is the optional phase-3 (b) source restriction (the host proxy injects
    # it as X-Loc-Trust-Secret); secret-flagged so export-env renders a <secret> placeholder.
    ("LOC_TMS_TRUST_PRINCIPAL", "", False),
    ("LOC_TMS_TRUST_SCOPES", "", False),  # blank = "api docs keys"
    ("LOC_TMS_TRUST_ROLE", "", False),  # blank = "viewer"
    ("LOC_TMS_TRUST_ORG_ID", "", False),  # required when multi_org_enabled=1
    ("LOC_TMS_TRUST_HEADER_SECRET", "", True),  # optional (b) source restriction; ASCII, >=32
    ("LOC_TMS_DB_ENCRYPTION", "", False),  # "sqlcipher" for at-rest encryption
    # Secrets. config._secret also honors the UNPREFIXED SESSION_SECRET /
    # ENCRYPTION_KEY spellings on non-embedded installs (prefixed wins on
    # conflict; an existing keyfile out-ranks the unprefixed var); embedded mode
    # honors ONLY these prefixed names. This scaffold pins the prefixed spelling
    # because it works in every mode. See docs/SECRETS.md.
    ("LOC_TMS_SESSION_SECRET", "", True),
    ("LOC_TMS_ENCRYPTION_KEY", "", True),
    ("ANTHROPIC_API_KEY", "", True),
    ("GEMINI_API_KEY", "", True),
    ("OPENAI_API_KEY", "", True),
    ("LOCAL_BASE_URL", "", False),
    ("DEFAULT_PROVIDER", "claude", False),  # or "mock" to run offline (no key needed)
    ("LOG_FORMAT", "text", False),
    ("LOC_TMS_ADMIN_EMAIL", "", False),  # for `init --from-env`
    ("LOC_TMS_ADMIN_PASSWORD", "", True),
]


def register(subparsers) -> None:
    cfg = subparsers.add_parser(
        "config", help="Read/write app settings (same store as the Settings UI)."
    )
    sub = cfg.add_subparsers(dest="config_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("show", help="Read configured private egress policy (local or --remote).")
    p.set_defaults(func=_show)

    p = sub.add_parser(
        "get", help="Print one setting's value (secrets show '(set)'/'(unset)', never plaintext)."
    )
    p.add_argument("key", help="the setting key (must be a known key).")
    p.set_defaults(func=_get)

    p = sub.add_parser("set", help="Set one setting (validated against known keys).")
    p.add_argument("key", help="the setting key.")
    p.add_argument("value", help="the new value.")
    p.add_argument(
        "--yes", action="store_true", help="skip the confirmation on security-sensitive keys."
    )
    p.set_defaults(func=_set)

    p = sub.add_parser("list", help="List all settings, grouped, secrets masked.")
    p.add_argument("--group", help=f"only show one group ({', '.join(_GROUPS)}).")
    p.set_defaults(func=_list)

    p = sub.add_parser(
        "apply",
        help="Apply many settings from a TOML file "
        "(all keys validated first, then one atomic write).",
    )
    p.add_argument("file", help="path to a .toml file of key = value pairs.")
    p.add_argument("--dry-run", action="store_true", help="show the diff without writing anything.")
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation when the file touches sensitive keys.",
    )
    p.set_defaults(func=_apply)

    p = sub.add_parser(
        "export-env",
        help="Print a .env scaffold of the install-time env vars (secrets as placeholders).",
    )
    p.set_defaults(func=_export_env)

    p = sub.add_parser(
        "features",
        help="List the toggleable features + their on/off state "
        "(remote-capable; flip one with `config feature enable|disable <slug>`).",
    )
    p.set_defaults(func=_features)

    # `config feature <slug>` -- the WRITE twin of `config features`, and the only
    # flag-write door that works headlessly against a remote install (`config set` is
    # require_local, and the Settings card needs a browser). enable/disable mirrors
    # `enterprise enable|disable` and `admin semantic enable|disable`.
    feat = sub.add_parser(
        "feature",
        help="Turn one feature on or off by slug (the write twin of `config features`).",
    )
    fsub = feat.add_subparsers(dest="feature_cmd", metavar="<subcommand>")
    fsub.required = True
    for verb, on in (("enable", True), ("disable", False)):
        fp = fsub.add_parser(verb, help=f"Turn one feature {'on' if on else 'off'}.")
        fp.add_argument("slug", help="the feature slug (see `config features`).")
        fp.set_defaults(func=_feature_enable if on else _feature_disable)


def _known_keys() -> dict:
    from app.kernel import settings

    return settings.DEFAULTS


def _validate_key(key: str) -> None:
    if key not in _known_keys():
        raise CliError(f"unknown setting key: {key!r} (see `config list`).", EXIT_USAGE)


def _display_value(key: str, *, capacity=False):
    """Read through the shared masked settings seam."""
    if capacity:
        from app import config

        value = getattr(config, key.removeprefix("LOC_TMS_"))
        return ("(set)" if value else "(unset)") if is_secret_key(key) else value
    from app.kernel import settings

    return settings.display_value(key)


def _get(args, emit) -> int:
    require_local(args)
    _validate_key(args.key)
    value = _display_value(args.key)
    if emit.as_json:
        emit.json({"ok": True, "key": args.key, "value": value, "secret": is_secret_key(args.key)})
    else:
        emit.say(f"{args.key} = {value}")
    return 0


# --- config set --------------------------------------------------------------
def _set(args, emit) -> int:
    require_local(args)
    _validate_key(args.key)
    from app.kernel import audit, settings

    key, value = args.key, args.value

    # Per-key value validation (P3 M1/M5): this HOST door also consults the shared
    # value-validator registry, so `config set advanced_web_search_cost_usd 0` (and the
    # other money-bearing / egress keys) can never store a garbage price or an SSRF-blocked
    # host. A no-op for keys with no registered validator. Refuse BEFORE any write, and
    # before the confirm prompt (#280 made the password/session policy keys sensitive: a
    # bad value is a usage error, never a question worth asking).
    try:
        settings.validate_value(key, value)
    except settings.InvalidSettingValue as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc

    # #519: the prompt, the success line and the audit detail all show the MASKED value (a
    # secret as (set)/(unset), a credential-bearing URL without userinfo or query).
    masked = settings.mask_value(key, value)
    if key in _sensitive_keys() and not confirm(
        emit, f"Change security-sensitive setting {key!r} to {masked!r}?", args.yes
    ):
        raise CliError("aborted.", 1)

    # Gate round 1 LOW-2: the actor names the operator in a slot-move audit row.
    actor = getattr(args, "actor", None) or "cli"
    if key in _encrypted_secret_keys():
        settings.set_secret(key, value)  # goes through set_many -> cache + apply
        shown = "(set)" if value else "(unset)"
    elif is_secret_key(key):
        settings.set_many({key: value}, actor=actor)
        shown = "(set)" if value else "(unset)"
    else:
        settings.set_many({key: value}, actor=actor)
        shown = masked

    audit.log(
        actor=actor,
        action="config.set",
        target=key,
        detail=("(secret changed)" if is_secret_key(key) else f"= {masked}"),
        # #220: switching audit logging (or its posture) is always recorded, even OFF.
        force=key in settings.AUDIT_POSTURE_KEYS,
    )
    emit.result(
        f"{key} = {shown}", {"ok": True, "key": key, "value": shown, "secret": is_secret_key(key)}
    )
    return 0


# --- config list -------------------------------------------------------------
def _list(args, emit) -> int:
    require_local(args)
    if args.group and args.group not in _GROUPS:
        raise CliError(f"unknown group: {args.group!r} ({', '.join(_GROUPS)}).", EXIT_USAGE)
    known = set(_known_keys())
    groups = {args.group: _GROUPS[args.group]} if args.group else dict(_GROUPS)
    if not args.group:
        placed = {k for keys in _GROUPS.values() for k in keys}
        leftover = sorted(known - placed)
        if leftover:
            groups["other"] = leftover

    payload = {}
    for gname, keys in groups.items():
        rows = []
        for key in keys:
            if gname == "capacity":
                from app import config

                if not key.startswith("LOC_TMS_") or not hasattr(
                    config, key.removeprefix("LOC_TMS_")
                ):
                    continue
            elif key not in known:
                continue
            value = _display_value(key, capacity=gname == "capacity")
            rows.append((key, value))
            payload[key] = value
        if rows and not emit.as_json:
            emit.say(f"[{gname}]")
            emit.table(rows)
            emit.say("")
    if emit.as_json:
        emit.json({"ok": True, "settings": payload})
    return 0


# --- config features / config feature enable|disable --------------------------
def _feature_rows(payload: list) -> list:
    """The human table for a features payload -- ONE renderer, so the local arm and the
    --remote arm print byte-identical tables from the same row dicts."""
    return [
        (
            r.get("slug", ""),
            r.get("title", ""),
            "on" if r.get("enabled") else "off",
            r.get("setting_key", ""),
            r.get("semantics", ""),
            # `door` wins the note: for SCIM/SAML both facts are true, and "the switch
            # is somewhere else" is the one that tells the operator what to do next.
            "moved" if r.get("door") else ("host-only" if r.get("local_only") else ""),
        )
        for r in payload
    ]


_FEATURE_HEADERS = ("slug", "title", "state", "setting", "semantics", "notes")


def _emit_features(emit, payload: list) -> int:
    if emit.as_json:
        emit.json({"ok": True, "features": payload})
    else:
        emit.table(_feature_rows(payload), headers=_FEATURE_HEADERS)
    return 0


def _features(args, emit) -> int:
    """The CLI twin of the Settings > Features card: list each toggleable feature, its
    live on/off state, backing setting key and semantics. Remote-capable (GET
    /api/admin/features) so a headless operator can read the switchboard of an install
    they are not sitting in front of; the write twin is `config feature enable|disable`."""
    if remote_mode(args):
        return _remote_impl.config_features(args, emit)
    from app.kernel import features_admin

    return _emit_features(emit, features_admin.list_features())


def _feature_enable(args, emit) -> int:
    return _feature_set(args, emit, True)


def _feature_disable(args, emit) -> int:
    return _feature_set(args, emit, False)


def _feature_set(args, emit, enabled: bool) -> int:
    """Flip one feature flag, locally or against a remote install.

    On SUCCESS both arms print the same line and the same --json object, because both
    carry the same four keys from the one seam (locally app.kernel.features_admin,
    remotely POST /api/admin/features/{slug}, which is a thin shell over that same seam).

    REFUSALS follow each arm's own convention, and that is not a parity bug: the local
    arm raises CliError with the CLI's own exit codes (an unknown slug is EXIT_USAGE, 2),
    while every remote 4xx becomes exit 1 carrying the server's message verbatim
    (_remote.RemoteClient._decode -- the whole CLI works that way, so an unknown slug is
    exit 2 locally and exit 1 remotely). Branch on the exit code being non-zero, never on
    which non-zero it is.

    NO CONFIRM PROMPT, and that is a checked fact rather than an oversight: the only
    feature keys on settings.SENSITIVE_KEYS are scim/saml, whose switch moved to
    /settings/enterprise -- this verb REFUSES them (naming that door) instead of
    confirming them. tests/verify_feature_flag_admin.py fails if a future feature key is
    sensitive without being door-moved or install-locked."""
    if remote_mode(args):
        return _remote_impl.config_feature_set(args, emit, enabled)
    from app.kernel import features_admin

    try:
        out = features_admin.set_feature_enabled(
            args.slug,
            enabled,
            actor=(getattr(args, "actor", None) or "cli"),
            remote=False,
        )
    except features_admin.FeatureNotFound as exc:
        raise CliError(
            f"unknown feature: {args.slug!r} (see `config features`).", EXIT_USAGE
        ) from exc
    except features_admin.FeatureAdminError as exc:
        raise CliError(str(exc)) from exc
    return _emit_feature_result(emit, out)


def _emit_feature_result(emit, out: dict) -> int:
    state = "on" if out.get("enabled") else "off"
    note = "" if out.get("changed") else " (no change)"
    emit.result(f"{out.get('slug')} = {state}{note}", {"ok": True, **out})
    return 0


# --- config apply ------------------------------------------------------------
def _apply(args, emit) -> int:
    require_local(args)
    from app.kernel import audit, settings

    try:
        with open(args.file, "rb") as fh:
            raw_bytes = fh.read()
    except FileNotFoundError as e:
        raise CliError(f"file not found: {args.file}") from e
    # TOML v1.0.0 treats a leading BOM as invisible, but tomllib does not strip it and
    # fails with 'Invalid statement (line 1, col 1)'. PowerShell's `-Encoding utf8`
    # (and many editors) prepend a UTF-8 BOM, so decode with utf-8-sig to drop it (a
    # no-op when absent) before parsing (#28). tomllib.load wants bytes; loads wants str.
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CliError(f"invalid TOML (not valid UTF-8): {exc}", EXIT_USAGE) from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CliError(f"invalid TOML: {exc}", EXIT_USAGE) from exc

    # Flatten: accept either a flat table or a single [settings] table.
    if list(data.keys()) == ["settings"] and isinstance(data["settings"], dict):
        data = data["settings"]

    known = set(_known_keys())
    unknown = [k for k in data if k not in known]
    if unknown:
        raise CliError(
            "unknown setting key(s): "
            + ", ".join(sorted(unknown))
            + " (rejecting the whole file).",
            EXIT_USAGE,
        )

    # Compute the diff (stringify - Setting values are strings).
    changes, diff_rows, proposed = {}, [], {}
    for key, raw in data.items():
        new_val = "1" if raw is True else "0" if raw is False else str(raw)
        proposed[key] = new_val
        old_disp = _display_value(key)
        new_disp = settings.mask_value(key, new_val)
        if is_secret_key(key) or settings.get(key) != new_val:
            changes[key] = new_val
            diff_rows.append((key, str(old_disp), new_disp))

    if not changes:
        emit.result(
            "No changes (file matches current settings).",
            {"ok": True, "changed": {}, "dry_run": args.dry_run},
        )
        return 0

    if not emit.as_json:
        emit.say("Changes:")
        emit.table(diff_rows, headers=("key", "current", "new"))
    if args.dry_run:
        emit.result(
            "Dry run - nothing written.", {"ok": True, "changed": list(changes), "dry_run": True}
        )
        return 0

    # Per-key value validation (P3 M1/M5): this HOST door validates the money-bearing /
    # egress keys through the shared registry BEFORE the atomic write, refusing the WHOLE
    # apply on a bad value (mirroring set_many's atomicity) so `config apply` cannot reopen
    # the unvalidated hole `config set` and the /api spine already close. A no-op for keys
    # with no registered validator.
    try:
        # Include unchanged file entries: a retained pin must cover a repointed base.
        settings.validate_values(proposed)
    except settings.InvalidSettingValue as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc

    sensitive = sorted(k for k in changes if k in _sensitive_keys())
    if sensitive and not confirm(
        emit, f"Apply changes to sensitive key(s) {', '.join(sensitive)}?", args.yes
    ):
        raise CliError("aborted.", 1)

    # One atomic set_many (fires cache invalidation + apply_to_config once). The
    # encrypted secret keys are Fernet-wrapped separately first.
    plain = {}
    for key, val in changes.items():
        if key in _encrypted_secret_keys():
            settings.set_secret(key, val)
        else:
            plain[key] = val
    actor = getattr(args, "actor", None) or "cli"
    if plain:
        # Gate round 1 LOW-2: the actor names the operator in a slot-move audit row.
        settings.set_many(plain, actor=actor)

    audit.log(
        actor=actor,
        action="config.apply",
        detail=f"{len(changes)} key(s): " + ", ".join(sorted(changes)),
        # #220: an apply that switches audit logging (or its posture) is always recorded.
        force=bool(settings.AUDIT_POSTURE_KEYS & set(changes)),
    )
    emit.result(
        f"Applied {len(changes)} setting(s).",
        {"ok": True, "changed": list(changes), "dry_run": False},
    )
    return 0


# --- config export-env -------------------------------------------------------
def _export_env(args, emit) -> int:
    require_local(args)
    from app import branding, config

    lines = [
        f"# {branding.brand_name()} environment scaffold (generated by `config export-env`).",
        "# Fill in secrets; blank vars fall back to their built-in defaults.",
        "",
    ]
    payload = {}
    for name, example, secret in _ENV_SCAFFOLD:
        # Prefer the live config value where we safely can (non-secret install knobs).
        current = ""
        if not secret:
            current = {
                "HOST": config.HOST,
                "PORT": str(config.PORT),
                "DEFAULT_PROVIDER": config.DEFAULT_PROVIDER,
                "LOG_FORMAT": config.LOG_FORMAT,
            }.get(name, example)
        val = "<secret>" if secret else current
        lines.append(f"{name}={val}")
        payload[name] = val
    if emit.as_json:
        emit.json({"ok": True, "env": payload})
    else:
        for line in lines:
            emit.say(line)
    return 0


def _show(args, emit) -> int:
    if remote_mode(args):
        payload = _remote_impl._client(args).get("/api/admin/config")["settings"][
            "egress_private_allowlist"
        ]
    else:
        from app.kernel import settings

        payload = settings.egress_policy_view()
    if emit.as_json:
        emit.json({"ok": True, "settings": {"egress_private_allowlist": payload}})
    else:
        emit.say(
            f"egress_private_allowlist = {payload['value']} "
            f"(source: {payload['source']}, valid: {payload['valid']})"
        )
    return 0
