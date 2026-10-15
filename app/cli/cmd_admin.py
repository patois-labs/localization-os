"""`admin` command group — Phase 7 (Intelligence / Admin).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _register_tick, _register_oidc, _register_gdpr,
_set_settings, _srx_status, _read_bytes, _retention_status,
_tick, _LDAP_KEYS, _ldap_config, _ldap_status, _prompt_password, _OIDC_KEYS, _oidc_config,
_oidc_status, _parse_since, _audit_verify, _gdpr_export, _gdpr_erase, _rule_add, _rule_list,
_rule_show, _rule_toggle, _rule_delete, _budget_set, _budget_list, _budget_delete,
_project_in_op_org, _project_budget_list, _project_budget_delete, _lqa_rulebook_get,
_lqa_rulebook_set from app.cli.cmd_admin_helpers.

The operator-facing surface for the features that were UI-only until now:
SRX segmentation, data retention, LDAP + OIDC identity, the audit
chain, GDPR subject rights, content-routing rules, department budgets, and LQA
rulebooks. Every subcommand is a thin CLI shell over app.services.* — the same
services the web forms drive, so there is one code path.

All in-process (local-root, no RBAC — same trust as editing the DB directly).
--remote is reserved for a later phase; every command calls require_local() so a
--remote invocation fails cleanly rather than silently running locally.

Secrets never surface. The LDAP bind password is stored Fernet-encrypted via
settings.set_secret (config.set_secret) and only ever shows (set)/(unset); the
SMTP password, OIDC client secret, and OAuth client secrets are masked the same
way (config's is_secret_key redaction). No secret is ever echoed or written to
--json.

Irreversible operations refuse to run blind. `retention purge-now` (deletes aged
uploads/documents/jobs/TM) and `gdpr erase` (anonymizes a user) both require an
interactive confirmation or --yes, and REFUSE in --json/--quiet without --yes
(confirm() returns False non-interactively). purge-now first echoes exactly what
it will remove so the operator sees the blast radius before answering.

Documented in: docs/architecture/connectors.md
"""

import os
import sys

from app.cli.cmd_admin_helpers import (
    _LDAP_KEYS,
    _OIDC_KEYS,
    _audit_verify,
    _budget_delete,
    _budget_list,
    _budget_set,
    _confirm_settings_write,
    _gdpr_erase,
    _gdpr_export,
    _ldap_config,
    _ldap_status,
    _lqa_rulebook_get,
    _lqa_rulebook_set,
    _oidc_config,
    _oidc_status,
    _parse_since,
    _project_budget_delete,
    _project_budget_list,
    _project_in_op_org,
    _prompt_password,
    _read_bytes,
    _register_gdpr,
    _register_oidc,
    _register_tick,
    _retention_status,
    _rule_add,
    _rule_delete,
    _rule_list,
    _rule_show,
    _rule_toggle,
    _set_settings,
    _srx_status,
    _tick,
)

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _resolve_user,
    _session,
    bounded_id,
    confirm,
    local_op,
    op_org_id,
    redact,
    remote_mode,
    require_operator,
    require_org_scope,
)

__all__ = [
    "_LDAP_KEYS",
    "_OIDC_KEYS",
    "_audit_verify",
    "_budget_delete",
    "_budget_list",
    "_budget_set",
    "_confirm_settings_write",
    "_gdpr_erase",
    "_gdpr_export",
    "_ldap_config",
    "_ldap_status",
    "_lqa_rulebook_get",
    "_lqa_rulebook_set",
    "_oidc_config",
    "_oidc_status",
    "_parse_since",
    "_project_budget_delete",
    "_project_budget_list",
    "_project_in_op_org",
    "_prompt_password",
    "_read_bytes",
    "_register_gdpr",
    "_register_oidc",
    "_register_tick",
    "_resolve_user",
    "_retention_status",
    "_rule_add",
    "_rule_delete",
    "_rule_list",
    "_rule_show",
    "_rule_toggle",
    "_set_settings",
    "_srx_status",
    "_tick",
    "op_org_id",
    "redact",
    "require_org_scope",
]


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "admin",
        help="Intelligence / admin ops (srx, retention, ldap, "
        "oidc, audit, gdpr, routing, budget, lqa).",
    )
    sub = grp.add_subparsers(dest="admin_group", metavar="<group>")
    sub.required = True

    _register_srx(sub)
    _register_retention(sub)
    _register_ldap(sub)
    _register_oidc(sub)
    _register_audit(sub)
    _register_gdpr(sub)
    _register_routing(sub)
    _register_budget(sub)
    _register_lqa(sub)
    _register_tick(sub)


def _register_srx(sub) -> None:
    grp = sub.add_parser(
        "srx", help="SRX 2.0 segmentation (enable/disable/status/validate/upload)."
    )
    s = grp.add_subparsers(dest="srx_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser("enable", help="Use the SRX rule engine for segmentation.")
    p.set_defaults(func=_srx_enable)
    p = s.add_parser("disable", help="Fall back to the legacy regex splitter.")
    p.set_defaults(func=_srx_disable)
    p = s.add_parser("status", help="Show the segmentation settings.")
    p.set_defaults(func=_srx_status)
    p = s.add_parser("validate", help="Parse-validate an .srx file (writes nothing).")
    p.add_argument("file", help="path to an .srx ruleset.")
    p.set_defaults(func=_srx_validate)
    p = s.add_parser("upload", help="Validate + install a custom .srx ruleset.")
    p.add_argument("file", help="path to an .srx ruleset.")
    p.set_defaults(func=_srx_upload)


def _register_retention(sub) -> None:
    grp = sub.add_parser("retention", help="Data retention (config/status/purge-now).")
    s = grp.add_subparsers(dest="retention_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser("config", help="Set the retention policy.")
    p.add_argument(
        "--enabled", choices=["on", "off"], default=None, help="turn the automated sweep on/off."
    )
    p.add_argument(
        "--days",
        type=int,
        default=None,
        help="retention window in days (0 = no window = never delete).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (these settings are security-sensitive; with "
        "no terminal nothing is written without it).",
    )
    p.set_defaults(func=_retention_config)
    p = s.add_parser("status", help="Show the retention policy.")
    p.set_defaults(func=_retention_status)
    p = s.add_parser("purge-now", help="Run one retention sweep NOW (DESTRUCTIVE, irreversible).")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_retention_purge)


def _register_ldap(sub) -> None:
    grp = sub.add_parser("ldap", help="LDAP / AD auth (config/status/test-login).")
    s = grp.add_subparsers(dest="ldap_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser("config", help="Set LDAP connection settings.")
    p.add_argument("--enabled", choices=["on", "off"], default=None)
    p.add_argument(
        "--host", default=None, help="ldaps://, ldap:// (required StartTLS), or bare host (LDAPS)."
    )
    p.add_argument("--ca-cert", default=None, help="PEM CA bundle path; blank uses system roots.")
    p.add_argument("--base-dn", default=None, help="search base DN.")
    p.add_argument("--bind-dn", default=None, help="service/bind account DN.")
    p.add_argument(
        "--bind-password",
        default=None,
        help="service-account password (stored ENCRYPTED; never echoed).",
    )
    p.add_argument(
        "--user-filter", default=None, help="search filter with a {username} placeholder."
    )
    p.add_argument("--default-role", default=None, help="role granted to first-time LDAP sign-ins.")
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (these settings are security-sensitive; with "
        "no terminal nothing is written without it).",
    )
    p.set_defaults(func=_ldap_config)
    p = s.add_parser("status", help="Show LDAP settings (secrets masked).")
    p.set_defaults(func=_ldap_status)
    p = s.add_parser("test-login", help="Attempt an LDAP login against the configured directory.")
    p.add_argument("user", help="username / email to authenticate.")
    p.add_argument("--password", default=None, help="the password (omit to be prompted securely).")
    p.set_defaults(func=_ldap_test_login)


def _register_audit(sub) -> None:
    grp = sub.add_parser("audit", help="Audit log (export/verify).")
    s = grp.add_subparsers(dest="audit_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser(
        "export", help="Stream the audit chain as NDJSON (oldest first) to stdout or -o FILE."
    )
    p.add_argument(
        "--since",
        default="",
        help="ISO-8601 lower bound (inclusive), e.g. 2026-01-01 or 2026-01-01T00:00:00.",
    )
    p.add_argument(
        "--actor",
        dest="filter_actor",
        metavar="EMAIL",
        default="",
        help="filter to one actor (email).",
    )
    p.add_argument("--action", default="", help="filter to actions containing this substring.")
    p.add_argument(
        "--customer",
        dest="customer_id",
        type=bounded_id,
        default=None,
        help="filter to one customer's rows (R2-3; matches the web /audit customer-scoped view).",
    )
    p.add_argument("-o", "--out", default="", help="write to FILE instead of stdout.")
    p.add_argument("--force", action="store_true", help="overwrite -o FILE if it exists.")
    p.set_defaults(func=_audit_export)
    p = s.add_parser("verify", help="Verify the keyed hash chain end to end.")
    p.set_defaults(func=_audit_verify)


def _register_routing(sub) -> None:
    grp = sub.add_parser("routing", help="Content routing rules (rule add/list/toggle/delete).")
    s = grp.add_subparsers(dest="routing_cmd", metavar="<subcommand>")
    s.required = True
    rule = s.add_parser("rule", help="Routing rules.")
    rsub = rule.add_subparsers(dest="rule_cmd", metavar="<subcommand>")
    rsub.required = True

    p = rsub.add_parser("add", help="Create a routing rule.")
    p.add_argument("--ext", default="", help="match file extension(s), comma-sep (blank = any).")
    p.add_argument(
        "--subject",
        default="",
        help="RESERVED for v2, REFUSED -- subject matching is not consulted at run time yet.",
    )
    p.add_argument("--department", default="", help="match submitter department (blank = any).")
    p.add_argument("--priority", type=int, default=100, help="lower wins (default 100).")
    p.add_argument("--project", type=bounded_id, default=None, help="target project id.")
    p.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="RESERVED for v2, REFUSED -- workflow targeting is not consulted at run time yet.",
    )
    p.add_argument(
        "--language-set",
        type=bounded_id,
        default=None,
        help="RESERVED for v2, REFUSED -- language-set targeting is not consulted at run time yet.",
    )
    p.add_argument("--provider", default="", help="provider override.")
    p.set_defaults(func=_rule_add)

    p = rsub.add_parser("list", help="List routing rules (priority order).")
    p.set_defaults(func=_rule_list)

    p = rsub.add_parser("show", help="Show one routing rule (full config read-back).")
    p.add_argument("rule_id", type=bounded_id, help="the rule id.")
    p.set_defaults(func=_rule_show)

    p = rsub.add_parser("toggle", help="Flip a rule active/inactive.")
    p.add_argument("rule_id", type=bounded_id, help="the rule id.")
    p.set_defaults(func=_rule_toggle)

    p = rsub.add_parser("delete", help="Delete a rule (DESTRUCTIVE).")
    p.add_argument("rule_id", type=bounded_id, help="the rule id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_rule_delete)


def _register_budget(sub) -> None:
    grp = sub.add_parser(
        "budget", help="Per-department token budgets + per-project money budgets (set/list/delete)."
    )
    s = grp.add_subparsers(dest="budget_cmd", metavar="<subcommand>")
    s.required = True
    p = s.add_parser("set", help="Create or update a department's monthly cap.")
    p.add_argument("department", help="department name (stored lower-cased).")
    p.add_argument("limit", type=int, help="monthly token limit (<=0 = unlimited).")
    p.set_defaults(func=_budget_set)
    p = s.add_parser("list", help="List department budgets + this month's spend.")
    p.set_defaults(func=_budget_list)
    p = s.add_parser("delete", help="Delete a department budget (DESTRUCTIVE).")
    p.add_argument("budget_id", type=bounded_id, help="the budget id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_budget_delete)
    # CV-2: per-PROJECT money budgets, nested as `admin budget project <verb>` so the
    # department verbs above stay at the top level. Local-only (like the department
    # budget verbs -- budget management touches tenant data; remote administration is
    # via the /api/projects/{id}/budget routes, which the MCP surface relays).
    pgrp = s.add_parser("project", help="Per-project monthly USD budgets (set/list/delete).")
    ps = pgrp.add_subparsers(dest="project_budget_cmd", metavar="<subcommand>")
    ps.required = True
    p = ps.add_parser("set", help="Create or update a project's monthly USD cap.")
    p.add_argument("project_id", type=bounded_id, help="the project id.")
    p.add_argument("limit", type=float, help="monthly USD limit (<=0 = unlimited).")
    p.set_defaults(func=_project_budget_set)
    p = ps.add_parser("list", help="List project budgets + this month's spend.")
    p.set_defaults(func=_project_budget_list)
    p = ps.add_parser("delete", help="Delete a project budget (DESTRUCTIVE).")
    p.add_argument("budget_id", type=bounded_id, help="the budget id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_project_budget_delete)


def _register_lqa(sub) -> None:
    grp = sub.add_parser("lqa", help="LQA rulebooks (rulebook get/set).")
    s = grp.add_subparsers(dest="lqa_cmd", metavar="<subcommand>")
    s.required = True
    rb = s.add_parser("rulebook", help="Per-language-pair LQA rulebook.")
    rsub = rb.add_subparsers(dest="rulebook_cmd", metavar="<subcommand>")
    rsub.required = True
    p = rsub.add_parser("get", help="Print a language-pair rulebook.")
    p.add_argument("source", help="source locale (e.g. en-US).")
    p.add_argument("target", help="target locale (e.g. fr-FR).")
    p.set_defaults(func=_lqa_rulebook_get)
    p = rsub.add_parser("set", help="Install a language-pair rulebook from a file.")
    p.add_argument("source", help="source locale.")
    p.add_argument("target", help="target locale.")
    p.add_argument("file", help="path to the rulebook (markdown/DSL).")
    p.set_defaults(func=_lqa_rulebook_set)


# --- admin srx ---------------------------------------------------------------
def _srx_enable(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    _set_settings({"srx_enabled": "1"}, args, "segmentation.policy", "enabled=1")
    emit.result("SRX segmentation enabled.", {"ok": True, "srx_enabled": "1"})
    return 0


def _srx_disable(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    _set_settings({"srx_enabled": "0"}, args, "segmentation.policy", "enabled=0")
    emit.result("SRX segmentation disabled (legacy splitter).", {"ok": True, "srx_enabled": "0"})
    return 0


def _srx_validate(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import srx

    try:
        raw = _read_bytes(args.file)
    except OSError as exc:
        raise CliError(f"could not read {args.file}: {exc}") from exc
    ok, msg = srx.validate(raw)
    if not ok:
        # A bad ruleset is a clean failure, not a crash.
        raise CliError(f"invalid SRX: {msg}")
    emit.result(f"Valid SRX. {msg}", {"ok": True, "message": msg})
    return 0


def _srx_upload(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import audit, settings, srx

    try:
        raw = _read_bytes(args.file)
    except OSError as exc:
        raise CliError(f"could not read {args.file}: {exc}") from exc
    # Validate BEFORE writing the custom path — an invalid file is rejected and
    # nothing is persisted (mirrors the /settings/segmentation upload route).
    ok, msg = srx.validate(raw)
    if not ok:
        raise CliError(f"rejected: {msg}")
    srx.write_custom_ruleset(raw)
    settings.set_many({"srx_file": "segmentation.srx"})
    audit.log(actor=_actor(args), action="segmentation.upload", detail=args.file)
    emit.result(
        f"Installed custom SRX ruleset ({len(raw)} bytes). {msg}",
        {"ok": True, "bytes": len(raw), "message": msg},
    )
    return 0


# --- admin retention ---------------------------------------------------------
def _retention_config(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import settings

    updates = {}
    if args.enabled is not None:
        updates["retention_enabled"] = "1" if args.enabled == "on" else "0"
    if args.days is not None:
        if args.days < 0:
            raise CliError("--days must be >= 0.", EXIT_USAGE)
        updates["retention_days"] = str(args.days)
    if updates:
        _confirm_settings_write(updates, args, emit)
        _set_settings(
            updates, args, "config.set", ", ".join(f"{k}={v}" for k, v in updates.items())
        )
    enabled = settings.get("retention_enabled") == "1"
    days = settings.get("retention_days")
    emit.result(
        f"Retention: {'on' if enabled else 'off'}, window {days} day(s).",
        {"ok": True, "retention_enabled": "1" if enabled else "0", "retention_days": days},
    )
    return 0


def _retention_purge(args, emit) -> int:
    # #515: install-wide, host-only: --org cannot scope it (it would act on every org).
    require_operator(args, emit)
    from app.kernel import retention

    # Echo the blast radius BEFORE asking: how many rows the sweep would remove.
    if not retention.enabled():
        raise CliError(
            "retention is disabled; enable it first "
            "(`admin retention config --enabled on --days N`)."
        )
    days = retention.retention_days()
    if days <= 0:
        raise CliError(
            "retention_days is 0 (no window); a purge would delete nothing. Set --days first."
        )
    preview = _retention_preview(days)
    emit.say(f"This will PERMANENTLY delete data older than {days} day(s):")
    emit.say(f"  documents: {preview['documents']}   segments: {preview['segments']}")
    emit.say(f"  terminal jobs: {preview['jobs']}   upload files: {preview['files']}")
    emit.say(f"  key-image blobs (soft-deleted / stale stubs): {preview['key_image_blobs']}")
    # THE REST OF THE BLAST RADIUS (2026-08-31). Deleting an aged document now takes its whole
    # run history with it -- before the FK fix such a document could not be deleted at ALL, so
    # this dialog honestly described a sweep that only ever reached untranslated uploads. It
    # must not keep describing that sweep now that the real one is much larger: an operator
    # confirming an IRREVERSIBLE install-wide delete has to be told what actually goes. Same
    # rule as the M-4 key_image_blobs counter -- the dialog tracks what purge does, or it lies.
    # Gated on documents > 0 (N2): with no aged documents NONE of this applies, and printing
    # it would overstate a sweep that will only touch jobs / key-image blobs.
    if preview["documents"]:
        emit.say("  ...and, for every document above, everything hanging off it:")
        emit.say("    workflow runs + step traces + fan-out batches (the full run history)")
        emit.say("    LQA results, post-edit episodes, query threads + posts")
        emit.say("    vendor dispatch legs anchored to those runs, INCLUDING any frozen")
        emit.say("      operator-delivery package they hold")
        emit.say("    Term candidates survive with provenance stripped; so do delivery")
        emit.say("    approvals that are not the document's (kept, with their run link cleared).")
        # The one effect that reaches OUTSIDE the purged set (N1) -- operators must not learn
        # this from a report that changed under them.
        emit.say("  A SURVIVING document's QA findings / post-edit episodes may lose their")
        emit.say("    vendor-leg attribution if they reference a leg this sweep erases.")
    emit.say("  TM ages by retention activity, including imports and edits;")
    emit.say("    TM matching surviving document sources and key catalogues is kept.")
    if not confirm(emit, "Proceed with the irreversible purge?", args.yes):
        raise CliError("aborted.", 1)
    result = retention.purge()
    if result.get("busy"):
        raise CliError("a retention purge is already running; try again shortly.", 1)
    # `documents_skipped` MUST reach the human line, not just the JSON. A sweep that skips
    # every document prints "removed 0 document(s)" -- which is exactly what a healthy sweep
    # with nothing to do prints, and exactly the invisible-failure mode this counter exists to
    # end (it is how the WorkflowRun FK bug survived unnoticed). Appended only when non-zero,
    # so the happy-path line is byte-identical to before.
    _skipped = result.get("documents_skipped", 0)
    _line = (
        f"Purge complete: removed {result['documents']} document(s), "
        f"{result['segments']} segment(s), {result['jobs']} job(s), "
        f"{result['tm'] + result.get('tm_aged', 0)} TM row(s), "
        f"{result['files']} file(s)."
    )
    if result.get("row_aging_failed"):
        _line += " WARNING: row aging failed partway; counts include committed batches only."
    if result.get("blob_deletes_pending"):
        _line += (
            f" Upload erasure is pending for {result['blob_deletes_pending']} blob(s);"
            " automatic retry scheduled."
        )
    if result.get("lab_datasets_erased"):
        _line += f" Erased {result['lab_datasets_erased']} Lab dataset(s) and their results/labels."
    if _skipped:
        _line += (
            f" WARNING: {_skipped} document(s) could NOT be purged and were skipped "
            "(see the server log for the failure)."
        )
    # The dialog tracks what purge does, or it lies: name the Advanced erasure totals on the human
    # line too (not just the JSON), appended only when non-zero so the happy path is unchanged.
    _adv = (
        result.get("advanced_findings_reaped", 0)
        + result.get("advanced_staged_erased", 0)
        + result.get("advanced_transcript_rows_erased", 0)
        + result.get("advanced_roster_previews_erased", 0)
        + result.get("advanced_units_unparseable", 0)
    )
    if _adv:
        _line += (
            f" Advanced tier: {result['advanced_findings_reaped']} finding(s) reaped, "
            f"{result['advanced_staged_erased']} staged target(s) severed, "
            f"{result['advanced_transcript_rows_erased']} transcript entry(ies) erased, "
            f"{result['advanced_roster_previews_erased']} roster preview(s) stripped."
        )
        # round-3 M2: name any malformed scripted roster units that were skipped (not erased).
        if result.get("advanced_units_unparseable", 0):
            _line += (
                f" {result['advanced_units_unparseable']} malformed roster unit(s) skipped "
                "(non-integer segment_id/key_id)."
            )
    emit.result(_line, {"ok": True, "removed": result})
    return 0


def _retention_preview(days: int) -> dict:
    """Count what a purge with the current window WOULD remove (aged documents,
    their segments, terminal jobs, upload files). Read-only — deletes nothing.

    The document predicate MIRRORS `retention.purge` EXACTLY, `fmt != "keys"` included.
    INTERNAL KEY DOCUMENTS ARE NEVER AGED OUT (SPEC-key-based-projects 2.5, A-F5): they are
    created once on the first key in a namespace and their `created_at` never moves, so on a
    key-based install every one of them is older than any window — and this preview, which
    lacked the filter, told the operator the sweep was about to destroy the entire key
    catalogue it will not touch. An over-reporting confirm dialog is the same defect class as
    the under-reporting one M-4 fixed: the dialog tracks what purge does, in both directions."""
    from datetime import datetime, timedelta

    from sqlalchemy import or_ as _or
    from sqlmodel import select

    from app.models import Document, Job, JobStatus, KeyImage, Segment

    cutoff = datetime.utcnow() - timedelta(days=days)
    with _session() as s:
        doc_ids = s.exec(
            select(Document.id).where(Document.created_at < cutoff, Document.fmt != "keys")
        ).all()
        seg_n = 0
        for did in doc_ids:
            seg_n += len(s.exec(select(Segment.id).where(Segment.document_id == did)).all())
        job_n = len(
            s.exec(
                select(Job.id).where(
                    Job.created_at < cutoff, Job.status.in_([JobStatus.COMPLETED, JobStatus.FAILED])
                )
            ).all()
        )
        # KeyImage rows the backstop would reap: long-soft-deleted OR stale ready=False
        # stubs (mirrors retention.purge so the confirm dialog does not under-report, M-4).
        ki_n = len(
            s.exec(
                select(KeyImage.id).where(
                    _or(
                        KeyImage.deleted_at.is_not(None) & (KeyImage.deleted_at < cutoff),
                        (KeyImage.ready == False) & (KeyImage.uploaded_at < cutoff),  # noqa: E712
                    )
                )
            ).all()
        )
    return {
        "documents": len(doc_ids),
        "segments": seg_n,
        "jobs": job_n,
        "files": len(doc_ids),
        "key_image_blobs": ki_n,
    }


def _ldap_test_login(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import ldap_auth

    password = _prompt_password(args.password)
    user, err = ldap_auth.try_login(args.user, password)
    if user is not None:
        emit.result(
            f"LDAP login OK: {user.email} (role {user.role}, auth {user.auth_source}).",
            {"ok": True, "email": user.email, "role": user.role, "auth_source": user.auth_source},
        )
        return 0
    if err:
        raise CliError(f"LDAP refused: {err}")
    # (None, None) = fall-through: no match / wrong password / LDAP disabled/outage.
    # Surface the last transport reason. Without it this command, the ONLY diagnostic an
    # operator has, reports the same generic string whether the password was wrong or the
    # DC certificate does not chain to a trusted root, which is the failure the #169
    # hardening newly introduces on upgrade.
    reason = ldap_auth.last_outage_reason()
    raise CliError(
        "LDAP login failed (no match, wrong password, or LDAP is disabled/unreachable)."
        + (f" Last LDAP error: {reason}." if reason else "")
    )


# --- admin audit -------------------------------------------------------------
def _audit_export(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.audit_export(args, emit)
    local_op(args, emit)
    import json

    from app.kernel import audit

    since = _parse_since(args.since) if args.since else None
    # AP-7: self-log the export BEFORE reading rows, so a failed read/write still
    # leaves a trace (and the event ships in its own export when it matches). Parity
    # with the web /audit export twin and the /api twin, which log it too.
    audit.log(
        actor=_actor(args),
        action="audit.export",
        detail="actor={} action={} since={} customer={}".format(
            args.filter_actor or "*",
            args.action or "*",
            args.since or "*",
            getattr(args, "customer_id", None) or "*",
        ),
        customer_id=getattr(args, "customer_id", None),
    )
    # Multi-org P8: confine the export to the operator's resolved org scope
    # (args._op_org_id via --org). None (--operator / single-tenant, gate off) is
    # unscoped -> byte-identical to today. Mirrors the web /audit export twin.
    lines = []
    for rec in audit.iter_export(
        actor=args.filter_actor or None,
        action=args.action or None,
        since=since,
        org_id=getattr(args, "_op_org_id", None),
        customer_id=getattr(args, "customer_id", None),
    ):
        lines.append(json.dumps(rec, ensure_ascii=False, default=str))
    body = "\n".join(lines) + ("\n" if lines else "")
    if args.out:
        if os.path.exists(args.out) and not args.force:
            raise CliError(
                f"destination exists: {args.out} (pass --force to overwrite).", EXIT_USAGE
            )
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        emit.result(
            f"Wrote audit export -> {args.out} ({len(lines)} record(s)).",
            {"ok": True, "out": args.out, "records": len(lines)},
        )
    elif emit.as_json:
        emit.json({"ok": True, "records": len(lines), "rows": [json.loads(ln) for ln in lines]})
    else:
        sys.stdout.write(body)
    return 0


def _project_budget_set(args, emit) -> int:
    local_op(args, emit)
    from app.kernel import project_budgets

    caller_org_id = getattr(args, "_op_org_id", None)
    with _session() as session:
        proj = _project_in_op_org(session, args.project_id, caller_org_id)
        if proj is None:
            raise CliError(f"no such project: {args.project_id}", EXIT_USAGE)
        try:
            row, created = project_budgets.upsert_project_budget(
                session, proj.id, args.limit, actor=_actor(args)
            )
        except project_budgets.BudgetLimitError as e:
            raise CliError(str(e), EXIT_USAGE) from e
        emit.result(
            f"{'Created' if created else 'Updated'} project budget #{row.id}: "
            f"project {row.project_id} = ${row.monthly_usd_limit:,.2f}/month"
            + (" (unlimited)" if row.monthly_usd_limit <= 0 else "")
            + ".",
            {
                "ok": True,
                "id": row.id,
                "project_id": row.project_id,
                "monthly_usd_limit": row.monthly_usd_limit,
                "created": created,
            },
        )
    return 0
