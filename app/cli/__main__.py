"""Root argparse dispatcher for `python -m app.cli`.

Global flags apply to every command:
    --json            machine-readable output (JSON to stdout)
    --quiet / -q      suppress normal human output
    --verbose / -v    extra detail
    --actor EMAIL     audit attribution (default: "cli")
    --remote URL      route the command to a live server's /api (thin HTTP client,
                      org-scoped ENTIRELY by the key server-side; never super-admin)
    --token TOKEN     an lsu_ per-user API key, required with --remote (visible in
                      process listings and shell history; prefer --token-file or the
                      LOC_TMS_TOKEN env var)
    --token-file PATH read the lsu_ key from PATH's first line (stays out of argv)
    --insecure        allow --remote over plain http to a non-loopback, non-tailnet
                      host (the key is then sent unencrypted; prefer https://)
    --org ID          (SaaS in-process) scope the command to one org + stamp new rows
    --operator        (SaaS in-process) authorize an UNSCOPED cross-org run, backed by
                      env LOC_TMS_OPERATOR_KEY vs the stored operator_key_hash

Exit codes: 0 ok, 1 op-failure, 2 usage, 3 remote unreachable.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import argparse
import sys

from . import (
    _scope,
    cmd_account,
    cmd_admin,
    cmd_ask,
    cmd_config,
    cmd_connector,
    cmd_content_type,
    cmd_context,
    cmd_db,
    cmd_doc,
    cmd_engine,
    cmd_enterprise,
    cmd_export,
    cmd_field,
    cmd_formality,
    cmd_image,
    cmd_init,
    cmd_job,
    cmd_keys,
    cmd_lang,
    cmd_layout,
    cmd_lqa,
    cmd_mcp,
    cmd_notify,
    cmd_org,
    cmd_project,
    cmd_prompt_tap,
    cmd_providers,
    cmd_query,
    cmd_release,
    cmd_report,
    cmd_routing,
    cmd_scim,
    cmd_search,
    cmd_segprofile,
    cmd_style_guide,
    cmd_tagging,
    cmd_template,
    cmd_user,
    cmd_vendor,
    cmd_work,
    cmd_workflow,
)
from ._common import EXIT_FAIL, EXIT_OK, EXIT_USAGE, CliError, Emitter


def _register_optional(subparsers, name: str) -> None:
    """Register an OPTIONAL add-on's command group (Databricks, Advanced, LLMemory, the
    Evaluations Lab) when its module is present; skip it silently when the add-on is not
    installed (app/editions.py). The position in build_parser() is unchanged, so the command
    order is byte-identical with the add-on present."""
    import importlib

    from app.extensions import addon_missing

    try:
        module = importlib.import_module(f"{__package__}.{name}")
    except ModuleNotFoundError as exc:
        if not addon_missing(exc):
            raise
        return
    module.register(subparsers)


def build_parser() -> argparse.ArgumentParser:
    from app import branding

    # allow_abbrev=False: on Python 3.10/3.11 a subcommand's `--to` otherwise matches the
    # top-level `--token` / `--token-file` as an ambiguous abbreviation.
    parser = argparse.ArgumentParser(
        prog="loc-tms",
        allow_abbrev=False,
        description=f"{branding.brand_name()} headless CLI - in-process ops, config, and "
        "bootstrap (works with no server running).",
        epilog="In-process mode is a LOCAL-ROOT operation (same trust as editing "
        "the DB directly): no login, no RBAC, audit actor is 'cli' unless "
        "--actor is given. --remote URL --token lsu_... runs remote-capable "
        "commands as a thin HTTP client of /api, org-scoped by the token.",
    )
    # Global flags - parsed at the root so they precede the subcommand.
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="emit machine-readable JSON."
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress normal output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="print extra detail.")
    parser.add_argument(
        "--actor", default="cli", help="audit attribution for actions (default: cli)."
    )
    parser.add_argument(
        "--remote",
        metavar="URL",
        help="route a remote-capable command to a live server's /api "
        "(org-scoped by the key; never super-admin).",
    )
    parser.add_argument(
        "--token",
        metavar="TOKEN",
        help="an lsu_ per-user API key (required with --remote; visible in "
        "process listings and shell history, so prefer --token-file PATH or "
        "the LOC_TMS_TOKEN env var).",
    )
    parser.add_argument(
        "--token-file",
        metavar="PATH",
        help="read the lsu_ key from PATH's first line (an argv-free --token "
        "alternative; beats the LOC_TMS_TOKEN env var).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="allow --remote over plain http to a host that is not loopback or "
        "the tailnet (the key is then sent unencrypted; prefer https://).",
    )
    parser.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="(with --remote) send KEY as the Idempotency-Key of this command's paid call: "
        "re-running the same command with the same KEY within 24 h returns the first run's "
        "job or answer instead of spending again.",
    )
    parser.add_argument(
        "--org",
        metavar="ID",
        help="(SaaS in-process) scope the command to one org id + "
        "stamp new rows. No-op single-tenant.",
    )
    parser.add_argument(
        "--operator",
        action="store_true",
        help="(SaaS in-process) authorize an UNSCOPED cross-org run "
        "(env LOC_TMS_OPERATOR_KEY vs operator_key_hash).",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    cmd_release.register(subparsers)
    cmd_db.register(subparsers)
    cmd_config.register(subparsers)
    cmd_enterprise.register(subparsers)
    cmd_init.register(subparsers)
    cmd_project.register(subparsers)
    cmd_content_type.register(subparsers)
    cmd_field.register(subparsers)
    cmd_vendor.register(subparsers)
    cmd_style_guide.register(subparsers)
    cmd_lang.register(subparsers)
    cmd_doc.register(subparsers)
    cmd_image.register(subparsers)
    cmd_keys.register(subparsers)
    cmd_connector.register(subparsers)
    cmd_engine.register(subparsers)
    cmd_providers.register(subparsers)
    cmd_segprofile.register(subparsers)
    cmd_context.register(subparsers)
    cmd_template.register(subparsers)
    cmd_user.register(subparsers)
    cmd_account.register(subparsers)
    cmd_layout.register(subparsers)
    cmd_search.register(subparsers)
    cmd_formality.register(subparsers)
    cmd_notify.register(subparsers)
    cmd_job.register(subparsers)
    cmd_workflow.register(subparsers)
    cmd_admin.register(subparsers)
    cmd_org.register(subparsers)
    cmd_report.register(subparsers)
    cmd_query.register(subparsers)
    cmd_work.register(subparsers)
    cmd_lqa.register(subparsers)
    cmd_routing.register(subparsers)
    cmd_prompt_tap.register(subparsers)
    from . import cmd_ledger

    cmd_ledger.register(subparsers)
    _register_optional(subparsers, "cmd_databricks")
    cmd_export.register(subparsers)
    _register_optional(subparsers, "cmd_advanced")
    cmd_scim.register(subparsers)
    cmd_tagging.register(subparsers)
    _register_optional(subparsers, "cmd_memory")
    _register_optional(subparsers, "cmd_eval")
    cmd_ask.register(subparsers)
    cmd_mcp.register(subparsers)
    from . import cmd_mcp_oauth

    cmd_mcp_oauth.register(subparsers)
    # #556: record each leaf's verb path so main() can apply the central scope gate.
    _scope.stamp_leaves(parser)
    return parser


def _force_utf8_streams() -> None:
    """Emit real UTF-8 for user DATA (names, terms, footers) on every console.

    The mojibake bug (#15): a Windows pipe/redirect reports its encoding as cp1252
    while the console code page is often UTF-8 (65001). cp1252-encodable characters
    (em/en-dash, ellipsis, middot, accented Latin) then encode to single legacy
    bytes that are invalid standalone UTF-8 and render as garbage; only chars
    OUTSIDE cp1252 were escaped by the ASCII-safe wrapper. Reconfiguring the real
    std streams to UTF-8 at startup makes DATA render correctly, and — because
    argparse's --help printer writes straight to sys.stdout, bypassing the wrapper —
    also fixes help-text mojibake at the source.

    Guarded so it is a strict no-op where reconfigure does not apply:
      * StringIO / capture objects (the in-process verify suites redirect stdout to
        io.StringIO, which has no reconfigure) -> AttributeError -> skipped; those
        already accept arbitrary unicode directly.
      * --json is unaffected: json.dumps(ensure_ascii=True) already emits pure ASCII.
      * The subprocess verify harness captures bytes and only checks a trailing ASCII
        'OK' line, so UTF-8 stdout stays stable for it.
    errors='backslashreplace' is a belt-and-suspenders backstop (UTF-8 can encode all
    of Unicode, so it never actually fires on the UTF-8 target); the _write_ascii_safe
    wrapper remains the backstop when reconfigure could not run at all.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:  # noqa: SIM105
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):  # pragma: no cover - detached/odd stream
            pass


#: CLI groups that must stay usable while S3 is enabled-but-misconfigured (NEW-1). Both are
#: BLOB-FREE recovery surfaces: ``cmd_config`` touches only Settings/env (no BlobStore call),
#: and ``cmd_db`` (backup/restore/migrate/init-schema) works the DB file and merely PRINTS an
#: uploads reminder. They are the ONLY way to fix the misconfiguration (edit the Settings, flip
#: the switch off, migrate the schema) from the CLI, so a misconfigured S3 boot must NOT brick
#: them -- they proceed WITHOUT the S3 override plus a loud stderr warning instead of raising.
_BLOBSTORE_RECOVERY_GROUPS = frozenset({"config", "db"})


def _configure_blobstore_at_boot(args) -> None:
    """Register the S3 BlobStore override in-process for SURFACE PARITY with the server's
    ``_startup`` (BLOCKER-1). ``configure_at_boot`` had exactly one caller (``app/main.py``'s
    startup event); the CLI is a server-less in-process entrypoint, so without this an
    S3-enabled install had every CLI blob op silently resolve ``DiskBlobStore`` -- ``doc upload``
    would write to ``data/uploads/`` while the web app exports from S3 (a spurious 409 "delete
    and re-upload"), and ``admin gdpr erase`` / ``retention purge-now`` would delete from disk
    while the blob sat in the bucket (a GDPR erase that does not erase). This mirrors the
    ``install_default_resolver()`` "for surface parity" precedent in ``main()``.

    SKIPPED for ``--remote``: a thin HTTP client of ``/api`` needs no host creds and no local DB;
    the server it talks to owns its own blobstore. GUARDED on a ready schema: a fresh-box
    bootstrap verb (``init`` / ``db migrate`` / ``db init-schema``) has no ``setting`` table yet
    and does NO blob I/O, so registration is correctly a no-op there and the verb's own
    ``init_db`` builds the schema (an unguarded ``settings.get`` would crash the very first
    command on a new install with "no such table: setting").

    FAIL-LOUD, WITH A RECOVERY CARVE-OUT (NEW-1). On a READY DB an enabled-but-misconfigured
    install raises ``S3BlobStoreError`` out of ``configure_at_boot``. This helper is called
    INSIDE ``main()``'s try, so for a normal (blob-touching) verb that error becomes a clean
    ``CliError`` -- a proper exit code + the actionable message + valid ``--json`` -- never an
    uncaught traceback, and never a silent fall-through to disk. But the ``config`` and ``db``
    RECOVERY groups (both blob-free) must stay usable in exactly that state, or the operator is
    trapped (the four ``blobstore_s3_*`` topology/credential keys are REMOTE_IMMUTABLE with no
    ``--remote`` and no web form -- the ONLY fix is a local ``config``/``db`` verb): for those,
    log a loud stderr
    warning and proceed WITHOUT the S3 override. Same trap a cron missing env creds would hit on
    every unrelated verb -- the carve-out keeps the recovery path open."""
    if getattr(args, "remote", None):
        return
    from sqlalchemy import inspect as _sa_inspect

    from app.db import engine as _engine

    if not _sa_inspect(_engine).has_table("setting"):
        return  # schema not initialized yet -> the bootstrap verb builds it
    from app import extensions as _extensions

    s3_blobstore = _extensions.use("blobstore_s3")
    if not s3_blobstore.installed:
        return  # the S3 add-on is not installed: the default disk stores stay

    try:
        s3_blobstore.configure_at_boot()
    except s3_blobstore.S3BlobStoreError as exc:
        if getattr(args, "command", None) in _BLOBSTORE_RECOVERY_GROUPS:
            # Recovery carve-out: warn LOUDLY (stderr, so --json stdout stays valid; unconditional
            # so --quiet cannot hide a data-integrity warning) and run the verb WITHOUT S3.
            sys.stderr.write(
                "warning: S3 blobstore is ENABLED but misconfigured, so it is NOT active for this "
                "'{}' command -- {}\n  (Proceeding without S3 because '{}' is a recovery surface. "
                "Fix the configuration or set blobstore_s3_enabled 0, then retry blob operations.)\n".format(  # noqa: E501
                    getattr(args, "command", "?"), exc, getattr(args, "command", "?")
                )
            )
            return
        raise CliError(
            f"S3 blobstore is enabled but misconfigured, so no blob operation can run: {exc}  "
            "Fix it with a `config set blobstore_s3_*` / the LOC_TMS_S3_* env, or "
            "`config set blobstore_s3_enabled 0` -- the `config` and `db` commands stay usable "
            "in this state."
        ) from exc
    # TC-01: register the S3 ExportStore adapter in-process too (exact parity with the server's
    # _startup, which calls s3_exportstore.configure_at_boot after s3_blobstore's). Without this a
    # CLI-context GDPR erase on an S3 install (gdpr erase-user / retention.delete_document /
    # customers.delete_customer -> data_export.purge_artifacts_covering) resolves the DEFAULT
    # DiskExportStore, so its delete() misses the S3 object and the erased subject's export ZIP
    # persists in the bucket. Same enable gate (blobstore_s3_enabled) + recovery carve-out as above.
    s3_exportstore = _extensions.use("exportstore_s3")
    if not s3_exportstore.installed:
        return

    try:
        s3_exportstore.configure_at_boot()
    except s3_exportstore.S3ExportStoreError as exc:
        if getattr(args, "command", None) in _BLOBSTORE_RECOVERY_GROUPS:
            sys.stderr.write(
                "warning: S3 export staging is ENABLED but misconfigured, so it is NOT active for "
                "this '{}' command -- {}\n  (Proceeding without S3 export staging as '{}' is a "
                "recovery surface. Fix the configuration or set blobstore_s3_enabled 0.)\n".format(
                    getattr(args, "command", "?"), exc, getattr(args, "command", "?")
                )
            )
            return
        raise CliError(
            f"S3 export staging is enabled but misconfigured, so no export operation can run: {exc}"
            "  Fix it with a `config set blobstore_s3_*` / the LOC_TMS_S3_* env, or "
            "`config set blobstore_s3_enabled 0` -- the `config` and `db` commands stay usable "
            "in this state."
        ) from exc


def _configure_jobqueue_at_boot(args) -> None:
    """Arm the durable JobQueue backend override in-process for SURFACE PARITY with the
    server's ``_startup`` (round-2 gate SHOULD-FIX-B). ``jobqueue.configure_at_boot`` had
    exactly one caller (``app/main.py``'s startup event); the CLI is a server-less in-process
    entrypoint, so without this a ``job_queue_backend=db`` install had every CLI job op resolve
    the DEFAULT ``InMemoryJobQueue`` -- and ``db recover-jobs`` (documented as runnable WITHOUT
    bouncing the server) would then run ``InMemoryJobQueue.recover()``, marking every RUNNING
    Job FAILED (including jobs actively running on the LIVE server) and never touching the
    durable ``jobqueue`` table. This mirrors ``_configure_blobstore_at_boot`` exactly.

    SKIPPED for ``--remote`` (a thin HTTP client of ``/api`` owns no local queue). GUARDED on a
    ready schema: a fresh-box bootstrap verb (``init`` / ``db migrate`` / ``db init-schema``)
    has no ``setting`` table yet and does no job I/O, so arming is correctly a no-op there.

    FAIL-LOUD, WITH THE SAME RECOVERY CARVE-OUT as blobstore. ``job_queue_backend`` is
    REMOTE_IMMUTABLE, so the ONLY way to fix a misconfigured value (set it back to ``memory``,
    or run ``db migrate`` to create the missing table) is a local ``config`` / ``db`` verb --
    so on a ``JobQueueError`` those recovery groups warn LOUDLY on stderr and proceed on the
    in-memory default (never trapping the operator), while any other verb surfaces a clean
    ``CliError`` rather than an uncaught traceback."""
    if getattr(args, "remote", None):
        return
    from sqlalchemy import inspect as _sa_inspect

    from app.db import engine as _engine

    if not _sa_inspect(_engine).has_table("setting"):
        return  # schema not initialized yet -> the bootstrap verb builds it
    from app.ports import jobqueue

    try:
        jobqueue.configure_at_boot()
    except jobqueue.JobQueueError as exc:
        # The same {config, db} recovery surfaces blobstore carves out: both are the only way
        # to fix a REMOTE_IMMUTABLE topology switch from the CLI, so they must stay usable.
        if getattr(args, "command", None) in _BLOBSTORE_RECOVERY_GROUPS:
            sys.stderr.write(
                "warning: job_queue_backend=db is selected but not usable, so the durable "
                "queue is NOT active for this '{}' command -- {}\n  (Proceeding on the in-memory "
                "queue because '{}' is a recovery surface. Fix it with `config set "
                "job_queue_backend memory` or `db migrate`, then retry.)\n".format(
                    getattr(args, "command", "?"), exc, getattr(args, "command", "?")
                )
            )
            return
        raise CliError(
            "job_queue_backend=db is selected but not usable, so no durable job operation can "
            f"run: {exc}  Fix it with `config set job_queue_backend memory` or `db migrate` -- "
            "the `config` and `db` commands stay usable in this state."
        ) from exc


def _apply_settings_at_boot(args) -> None:
    """Push stored settings into the live ``config`` module before any command runs — the
    in-process CLI mirror of the web server's ``_startup`` ``settings.apply_to_config()``
    call (issue #55). Without it a headless/scripted local-engine run went out with a BLANK
    local model id and the import-time env defaults: ``config.DEFAULT_MODELS["local"]``,
    ``config.LOCAL_BASE_URL``, ``config.LOCAL_API_KEY``, the stored ``default_provider``, and
    the in-app cloud-key overrides never left their env values, so ``_local_prompt_adapter("")``
    resolved to None, the qwen3.5/3.8 anti-thinking adapter never fired, and the call took the
    default chat path and burned ~80x the tokens / ~16x the wall clock for byte-equal output.
    (``default_provider`` was read live at resolve time, so the state was confusingly
    half-applied.) This is the SAME push the web app already does at startup, so after it the
    two surfaces resolve providers identically.

    SKIPPED for ``--remote``: that command is answered by a live server which applied its own
    settings at its own startup; the local ``config`` module is irrelevant to a thin HTTP
    client (and no local DB need exist). GUARDED on a ready schema (mirrors
    ``_configure_blobstore_at_boot``): a fresh-box bootstrap verb (``init`` / ``db migrate`` /
    ``db init-schema``) has no ``setting`` table yet, and ``apply_to_config()`` reads every key
    through ``settings.get()`` (a read-through that would raise ``no such table: setting``);
    the verb's own ``init_db`` builds the schema and nothing resolves a provider before it, so
    skipping here is correct AND keeps a fresh ``init`` from crashing. NEVER-CRASH: any failure
    to push is logged and swallowed — a settings apply must never turn a working command into a
    traceback (the same posture the cross-process re-apply in ``settings._probe_version`` takes)."""
    if getattr(args, "remote", None):
        return
    from sqlalchemy import inspect as _sa_inspect

    from app.db import engine as _engine

    if not _sa_inspect(_engine).has_table("setting"):
        return  # schema not initialized yet -> the bootstrap verb builds it
    from app.kernel import settings

    try:
        settings.apply_to_config()
    except Exception:
        import logging

        logging.getLogger("loc_tms.cli").warning(
            "settings.apply_to_config() at CLI bootstrap failed; provider resolution may fall "
            "back to import-time env defaults (blank local model, default base URL)",
            exc_info=True,
        )


def main(argv=None) -> int:
    _force_utf8_streams()
    # SSRF parity (P-F1 review F1): the in-process CLI drives connector pull/push, so it
    # needs the resolve-and-check layer the server installs at startup — else a plain DNS
    # name resolving to a private IP is allowed on this path (layers 1-2 still block every
    # numeric/localhost/.internal form). --remote routes through the server (already
    # resolver-on); this covers the local-root in-process arm for surface parity.
    from app.services.connectors.base import install_default_resolver

    install_default_resolver()
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)

    emit = Emitter(
        as_json=getattr(args, "as_json", False),
        quiet=getattr(args, "quiet", False),
        verbose=getattr(args, "verbose", False),
    )

    func = getattr(args, "func", None)
    if func is None:  # a bare group with no subcommand
        parser.error("a subcommand is required")
        return EXIT_USAGE

    try:
        # Surface parity (BLOCKER-1): register the S3 BlobStore override so an in-process CLI blob
        # op resolves the SAME adapter as the server. INSIDE the try (NEW-1) so an
        # enabled-but-misconfigured install surfaces a clean CliError on a blob-touching verb (not
        # an uncaught traceback), while the config/db recovery groups warn and proceed without S3.
        if args.command in {"version", "release"}:
            return func(args, emit) or EXIT_OK
        _configure_blobstore_at_boot(args)
        # Train S: arm the durable job-queue backend override so an in-process CLI job op (and
        # especially `db recover-jobs`) resolves the SAME adapter as the server on a
        # job_queue_backend=db install. INSIDE the try (like blobstore) so a misconfigured
        # install surfaces a clean CliError on a job verb, while config/db recovery groups warn.
        _configure_jobqueue_at_boot(args)
        _apply_settings_at_boot(args)
        _feature_dispatch_gate(args)
        _central_scope_gate(args, emit)
        return func(args, emit) or EXIT_OK
    except CliError as exc:
        if emit.as_json:
            obj = {"ok": False, "error": str(exc)}
            # A command may attach structured outcome fields (e.g. a precise apply
            # status); merge them WITHOUT letting them clobber ok/error.
            extra = getattr(exc, "payload", None)
            if extra:
                obj = {**extra, **obj}
            emit.json(obj)
        else:
            sys.stderr.write(f"error: {exc}\n")
        return exc.code
    except KeyboardInterrupt:
        sys.stderr.write("\naborted.\n")
        return EXIT_FAIL
    except Exception as exc:
        from app.filters.base import InputLimitError
        from app.output_safety import InvalidOutputURL

        if isinstance(exc, (InvalidOutputURL, InputLimitError)):
            if emit.as_json:
                emit.json({"ok": False, "error": str(exc)})
            else:
                sys.stderr.write(f"error: {exc}\n")
            return EXIT_USAGE
        # AI opt-out (DPAI-08): a refusal from any service is a designed outcome, not a
        # crash -- report it plainly as a usage error, never with a traceback.
        if type(exc).__name__ == "AIOptOutRefused" and isinstance(exc, ValueError):
            if emit.as_json:
                emit.json({"ok": False, "error": str(exc), "reason": "ai_opt_out"})
            else:
                sys.stderr.write(f"error: {exc}\n")
            return EXIT_USAGE
        if (
            type(exc).__module__ == "app.services.orchestrator"
            and type(exc).__name__ == "RunCapacityError"
            and hasattr(exc, "retry_after")
        ):
            if emit.as_json:
                emit.json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "reason": "run_capacity",
                        "code": "run_capacity",
                        "status": 503,
                        "retry_after": exc.retry_after,
                    }
                )
            else:
                sys.stderr.write(f"error: {exc}\n")
            return EXIT_FAIL
        # GitHub #213 part 2: a launch refused because ANOTHER workflow's run is in
        # flight on this (document, locale). Designed refusal, not a crash: the same
        # sentence the server's 409 body carries (the --remote arm prints it verbatim
        # too), non-zero exit, no traceback. Not retryable -- the message names the
        # run to cancel.
        if (
            type(exc).__module__ == "app.services.orchestrator"
            and type(exc).__name__ == "RunConflictError"
            and hasattr(exc, "run_id")
        ):
            if emit.as_json:
                emit.json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": "run_conflict",
                        "status": 409,
                        "run_id": exc.run_id,
                    }
                )
            else:
                sys.stderr.write(f"error: {exc}\n")
            return EXIT_FAIL

        try:
            from app import db
        except ImportError:
            raise exc from None
        if isinstance(exc, db.WriteContentionError):
            if emit.as_json:
                emit.json({"ok": False, "error": str(exc), "retry_after": exc.retry_after})
            else:
                sys.stderr.write(f"error: {exc}\n")
            return EXIT_FAIL
        if db.is_pool_timeout_error(exc):
            message = "db_pool_exhausted: " + db.POOL_EXHAUSTED_NOTE
            if emit.as_json:
                emit.json({"ok": False, "error": message, "reason": "db_pool_exhausted"})
            else:
                sys.stderr.write(f"error: {message}\n")
            return EXIT_FAIL
        hint = _stale_schema_hint(exc)
        if hint is None:
            raise  # not a schema-drift error -> keep the normal traceback
        if emit.as_json:
            emit.json({"ok": False, "error": hint, "schema_stale": True})
        else:
            sys.stderr.write(f"error: {hint}\n")
        # The raw SQLAlchemy traceback is still available under -v/--verbose.
        if emit.verbose:
            import traceback

            traceback.print_exc()
        return EXIT_FAIL


def _central_scope_gate(args, emit) -> None:
    """#556: apply the multi-org gate that matches the verb's class in app/cli/_scope.py, before
    the verb body runs. An org-scoped verb needs --org or --operator, an operator-only verb needs
    --operator; install-wide verbs run as before. A strict no-op with the gate off (both gates
    return at once) and in --remote mode (the key scopes it server-side; the verbs that cannot
    run remotely refuse it themselves). The per-verb require_* calls stay as a second wall."""
    from ._common import remote_mode, require_operator, require_org_scope

    if remote_mode(args):
        return
    verb = getattr(args, "_verb_path", "")
    if not verb:
        return
    scope = _scope.scope_of(verb)
    if scope == _scope.ORG_SCOPED:
        require_org_scope(args, emit)
    elif scope == _scope.OPERATOR_ONLY:
        require_operator(args, emit)


def _feature_dispatch_gate(args) -> None:
    """Feature kill-switch dispatch gate (defense in depth over the per-verb layer-2
    gates). Raises the feature's EXACT layer-2 CliError -- same message, same exit code
    -- so the shared handler below formats it identically whether layer 1 or layer 2
    fired (the behavior-neutral claim; verify_image_localization / verify_ask_draft pin
    both). In-process ONLY: a --remote command is answered by the server, whose own flag
    state is authoritative. Only REMOVED-semantics, default-ON features refuse (a
    default-OFF opt_in like prompt_tap has 'off' as its normal state; a writes_off
    feature keeps its reads). Imported here, not at build_parser, so the parser stays
    settings-free and verify_cli_parity's hermeticity holds."""
    if getattr(args, "remote", None):
        return
    from app import features

    feat = features.feature_for_cli_group(getattr(args, "command", None))
    if (
        feat is not None
        and feat.semantics == "removed"
        and feat.default_on()
        and not feat.is_enabled()
    ):
        msg = feat.cli_refusal or (
            f"{feat.title.lower()} is disabled (setting {feat.setting_key}=0)."
        )
        raise CliError(msg, feat.cli_exit_code)


def _stale_schema_hint(exc: BaseException):
    """If `exc` is a SQLite 'no such column/table' OperationalError — the tell-tale
    of a DB whose schema predates this code — return a friendly one-line message
    pointing at `db migrate`. Otherwise return None (let the error propagate).

    We match on the SQLAlchemy OperationalError message text so we catch the error
    however deep it was raised, without importing sqlite3's error type. Only the
    schema-drift shapes ('no such column', 'no such table') are treated as friendly;
    every other OperationalError (locked DB, disk I/O, etc.) still shows a traceback."""
    try:
        from sqlalchemy.exc import OperationalError
    except Exception:  # pragma: no cover - sqlalchemy always present at runtime
        return None
    if not isinstance(exc, OperationalError):
        return None
    msg = str(getattr(exc, "orig", exc) or exc).lower()
    if "no such column" not in msg and "no such table" not in msg:
        return None
    # Pull out the missing object name for a concrete message, if we can.
    detail = ""
    for marker in ("no such column: ", "no such table: "):
        if marker in msg:
            missing = msg.split(marker, 1)[1].split()[0].strip().rstrip(")")
            kind = "column" if "column" in marker else "table"
            detail = f" (missing {kind}: {missing})"
            break
    return (
        "your database schema is older than this code" + detail + ". "
        "Run:  loc-tms db migrate  -- then retry. "
        "(Started via run.py, the web app migrates automatically.)"
    )


if __name__ == "__main__":
    raise SystemExit(main())
