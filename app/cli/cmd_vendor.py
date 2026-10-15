"""Vendor profile, secret, customer links, rate card, archive, delete verbs and `vendor` group root.

Transitional compatibility shim for one release (added 2026-09-14, remove the
re-export block after the next tagged release): `__all__` re-exports `_actor`,
`_flag_gate`, `_fmt_rate` from `vendor_common` and `_leg_or_usage`, `_read_payload`,
`_vendor_approve_quote`, `_vendor_decline_quote`, `_vendor_dispatch_show`,
`_vendor_dispatches`, `_vendor_poll`, `_vendor_quote`, `_vendor_redrive`,
`_vendor_reject_rework`, `_vendor_return` from `vendor_jobs`.

`vendor` command group: first-class external translation/review vendors
(PROPOSAL-external-processing-step §9 decision 6; PROPOSAL-vendor-human-review-handoff;
combined build Wave A).

A vendor owns a name, a connection (endpoint URL + a Fernet-encrypted signing secret /
auth token, stored EXACTLY like connector credentials and NEVER echoed), its MSA rate
card (per TM-match band), and its customer links (the fail-closed scoping seam a
workflow's external step selects through). Business logic lives in
app.services.vendors + app.services.vendor_cost; this module is a thin CLI shell.

LOCAL-ONLY by design: there is no remote twin for the vendor group (it manages
credentials + rate cards on the host). Every verb calls require_local() first, so a
`--remote vendor ...` fails cleanly (exit 2) rather than silently running locally.

SECRETS: `set-secret` takes the credential off argv where possible (getpass / stdin,
the connector `--secret` convention); `show` reports only whether a secret is set,
never the value. ASCII-only output (no em-dashes/emoji -- Windows consoles).

Documented in: docs/architecture/vendor-outsourcing.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    op_org_id,
    op_org_scope,
    require_local,
    require_org_scope,
)
from .cmd_connector import _parse_secret_kv
from .vendor_common import (
    _actor,
    _flag_gate,
    _fmt_rate,
)
from .vendor_jobs import (
    _leg_or_usage,
    _read_payload,
    _vendor_approve_quote,
    _vendor_decline_quote,
    _vendor_dispatch_show,
    _vendor_dispatches,
    _vendor_poll,
    _vendor_quote,
    _vendor_redrive,
    _vendor_reject_rework,
    _vendor_return,
    register_jobs,
)

__all__ = [
    "EXIT_USAGE",
    "_VENDOR_SECRET_KEYS",
    "CliError",
    "_actor",
    "_bands",
    "_flag_gate",
    "_fmt_rate",
    "_get_vendor",
    "_leg_or_usage",
    "_parse_secret_kv",
    "_protocols",
    "_read_payload",
    "_session",
    "_vendor_add",
    "_vendor_approve_quote",
    "_vendor_archive",
    "_vendor_decline_quote",
    "_vendor_delete",
    "_vendor_dispatch_show",
    "_vendor_dispatches",
    "_vendor_link_customer",
    "_vendor_list",
    "_vendor_poll",
    "_vendor_quote",
    "_vendor_rate_card_set",
    "_vendor_rate_card_show",
    "_vendor_redrive",
    "_vendor_reject_rework",
    "_vendor_return",
    "_vendor_set_secret",
    "_vendor_show",
    "_vendor_unarchive",
    "_vendor_unlink_customer",
    "_vendor_update",
    "bounded_id",
    "confirm",
    "op_org_id",
    "op_org_scope",
    "register",
    "require_local",
    "require_org_scope",
]

# The only credential fields a vendor secret carries (signing the outbound dispatch
# request). An unknown key is refused so a typo never lands a dead field at rest.
_VENDOR_SECRET_KEYS = ("signing_secret", "auth_token")


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "vendor",
        help="External translation/review vendors (LOCAL-only): CRUD, "
        "secrets, customer links, MSA rate card, archive/delete.",
    )
    sub = grp.add_subparsers(dest="vendor_cmd", metavar="<subcommand>")
    sub.required = True  # a bare `vendor` shows the SCOPED usage (house convention).

    p = sub.add_parser(
        "add",
        help="Create a vendor (only NAME is required; fill in the connection + rate card later).",
    )
    p.add_argument("name", help="vendor display name.")
    p.add_argument("--description", default="", help="optional description.")
    p.add_argument(
        "--endpoint-url",
        dest="endpoint_url",
        default="",
        help="the vendor's intake endpoint URL (optional).",
    )
    p.add_argument(
        "--protocol",
        default="native",
        choices=list(_protocols()),
        help="transport protocol (default native: our signed JSON contract POSTed "
        "to the endpoint). manual: file-exchange, no wire -- deliver by "
        "downloading the leg's XLIFF and handing it off out-of-band; the "
        "translation returns via the guarded upload. A leg snapshots its "
        "protocol at dispatch, so flipping the vendor later never rewrites an "
        "in-flight leg. Per-LSP adapters are a later wave.",
    )
    p.set_defaults(func=_vendor_add)

    p = sub.add_parser("list", help="List vendors (active by default).")
    p.add_argument(
        "--include-archived",
        dest="include_archived",
        action="store_true",
        help="also show archived (retired) vendors.",
    )
    p.set_defaults(func=_vendor_list)

    p = sub.add_parser(
        "show",
        help="Show one vendor (connection state, rate card, "
        "customer links; never the secret value).",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.set_defaults(func=_vendor_show)

    p = sub.add_parser(
        "update",
        help="Update a vendor's name/description/endpoint (only the flags you pass change).",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.add_argument("--name", default=None, help="new name (unchanged if omitted).")
    p.add_argument(
        "--description", default=None, help="new description ('' clears it; unchanged if omitted)."
    )
    p.add_argument(
        "--endpoint-url",
        dest="endpoint_url",
        default=None,
        help="new endpoint URL ('' clears it; unchanged if omitted).",
    )
    p.add_argument(
        "--config-json",
        dest="config_json",
        default=None,
        help="non-secret transport config as a JSON object (e.g. "
        '\'{"headers": {"X-Account": "acme"}}\'); \'\' clears it. '
        "Config headers are sanitized at dispatch (Host / auth / our "
        "x-locstudio-* headers can never be overridden).",
    )
    p.add_argument(
        "--protocol",
        default=None,
        choices=list(_protocols()),
        help="transport protocol (unchanged if omitted). manual: file-exchange, no "
        "wire -- deliver by downloading the leg's XLIFF; the translation returns "
        "via the guarded upload. A leg snapshots its protocol at dispatch, so "
        "changing this only affects FUTURE dispatches.",
    )
    p.set_defaults(func=_vendor_update)

    p = sub.add_parser(
        "set-secret",
        help="Set/merge a vendor credential (signing "
        "secret / auth token), kept off argv where possible.",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        required=True,
        metavar="K[=V]",
        help="a SECRET credential to merge (repeatable; keys: "
        + ", ".join(_VENDOR_SECRET_KEYS)
        + "). Preferred forms keep "
        "the value off the command line: `--secret signing_secret` "
        "prompts (getpass, not echoed); `--secret signing_secret=-` "
        "reads one line from stdin. `--secret signing_secret=VALUE` "
        "still works but warns (argv is visible in shell history and "
        "`ps`). An explicit `--secret signing_secret=` CLEARS that field.",
    )
    p.set_defaults(func=_vendor_set_secret)

    p = sub.add_parser(
        "link-customer",
        help="Link a vendor to a customer (the "
        "fail-closed scope an external step selects through).",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.set_defaults(func=_vendor_link_customer)

    p = sub.add_parser("unlink-customer", help="Remove a vendor<->customer link.")
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.add_argument("customer_id", type=bounded_id, help="customer id.")
    p.set_defaults(func=_vendor_unlink_customer)

    # Nested rate-card subgroup: `vendor rate-card {set|show}` (house convention for a
    # nested subgroup -- a bare `vendor rate-card` shows the SCOPED usage/error).
    rc = sub.add_parser(
        "rate-card", help="Manage a vendor's MSA rate card (per TM-match band, cost per word)."
    )
    rc_sub = rc.add_subparsers(dest="vendor_rate_card_cmd", metavar="<subcommand>")
    rc_sub.required = True
    p = rc_sub.add_parser(
        "set",
        help="Set ONE band's rate (merged onto the existing card; the other bands are untouched).",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.add_argument(
        "--band", required=True, choices=list(_bands()), help="the rate-card band to set."
    )
    p.add_argument(
        "--rate",
        required=True,
        type=float,
        help="cost per word for this band (a float, e.g. 0.12).",
    )
    p.set_defaults(func=_vendor_rate_card_set)
    p = rc_sub.add_parser("show", help="Show a vendor's rate card (per-band rates).")
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.set_defaults(func=_vendor_rate_card_show)

    register_jobs(sub)

    p = sub.add_parser(
        "archive",
        help="Archive (retire) a vendor - drops it from "
        "every picker without breaking dispatch history.",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.set_defaults(func=_vendor_archive)

    p = sub.add_parser("unarchive", help="Unarchive (reactivate) a vendor.")
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.set_defaults(func=_vendor_unarchive)

    p = sub.add_parser(
        "delete",
        help="Delete a vendor (fails closed while a workflow "
        "step or dispatch still references it; never cascades).",
    )
    p.add_argument("vendor_id", type=bounded_id, help="vendor id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_vendor_delete)


# --- helpers -----------------------------------------------------------------
def _bands():
    from app.kernel import vendor_cost

    return vendor_cost.BANDS


def _protocols():
    from app.kernel import vendor_dispatch

    return vendor_dispatch.protocol_names()


def _get_vendor(session, args):
    """Fetch a vendor in the operator's --org scope, or 404-as-usage (an --org A
    operator must not see/mutate org B's vendor; includes archived rows).

    #73 COUPLING: op_org_SCOPE (not op_org_id) — vendors.visible now fails closed on a
    bare None under an active gate, so the key-verified --operator door must hand it
    access.UNSCOPED to keep the cross-org read; a bare None would 404 every vendor for
    the operator. Every vendor read/mutate CLI command funnels through here (or passes
    op_org_scope to the service, which re-checks via visible)."""
    from app.kernel import vendors

    v = vendors.visible(session, args.vendor_id, org_id=op_org_scope(args))
    if v is None:
        raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
    return v


# --- add ---------------------------------------------------------------------
def _vendor_add(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        try:
            v = vendors.create(
                session,
                args.name,
                description=args.description,
                endpoint_url=args.endpoint_url,
                protocol=getattr(args, "protocol", "native"),
                org_id=op_org_id(args),
                created_by=actor,
                actor=actor,
            )
        except vendors.VendorError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Created vendor #{v.id}: {v.name}", {"ok": True, **vendors.to_dict(session, v)}
        )
    return 0


# --- list --------------------------------------------------------------------
def _vendor_list(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    with _session() as session:
        rows_v = vendors.list_vendors(
            session, org_id=op_org_scope(args), include_archived=args.include_archived
        )
        if emit.as_json:
            emit.json({"ok": True, "vendors": [vendors.to_dict(session, v) for v in rows_v]})
        else:
            rows = [
                (v.id, v.name, v.endpoint_url or "-", "yes" if v.archived_at is not None else "-")
                for v in rows_v
            ]
            emit.table(rows, headers=("id", "name", "endpoint", "archived"))
    return 0


# --- show --------------------------------------------------------------------
def _vendor_show(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    with _session() as session:
        v = _get_vendor(session, args)
        d = vendors.to_dict(session, v)  # NEVER carries the secret value
        if emit.as_json:
            emit.json({"ok": True, **d})
        else:
            emit.say(f"Vendor #{d['id']}: {d['name']}" + ("  (archived)" if d["archived"] else ""))
            if d["description"]:
                emit.say(f"  description: {d['description']}")
            emit.say(f"  endpoint: {d['endpoint_url'] or '-'}")
            emit.say(f"  protocol: {d.get('protocol', 'native')}")
            emit.say(f"  secret: {'(set)' if d['has_secret'] else '(unset)'}")
            emit.say(f"  status: {d['status']}")
            if d["last_message"]:
                emit.say(f"  last message: {d['last_message']}")
            cids = d["customer_ids"]
            emit.say(
                "  linked customers: " + (", ".join(str(c) for c in cids) if cids else "(none)")
            )
            card = d["rate_card"]
            emit.say(f"  rate card ({card['currency']}, cost per word):")
            emit.table(
                [(b, _fmt_rate(card["bands"].get(b, 0.0))) for b in _bands()],
                headers=("band", "rate"),
            )
    return 0


# --- update ------------------------------------------------------------------
def _vendor_update(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        _get_vendor(session, args)  # clean 404 before the partial update
        # None-sentinel partial: only pass a field whose flag was actually given (an
        # explicit '' clears the description/endpoint text fields).
        kw = {}
        for f in ("name", "description", "endpoint_url", "protocol"):
            v = getattr(args, f, None)
            if v is not None:
                kw[f] = v
        # M1 parity: config is settable off /api too. '' clears it; a JSON object sets it.
        cj = getattr(args, "config_json", None)
        if cj is not None:
            if str(cj).strip() == "":
                kw["config_json"] = {}  # empty -> vendors.update stores NULL (clear)
            else:
                import json as _json

                try:
                    parsed = _json.loads(cj)
                except ValueError as exc:
                    raise CliError(f"--config-json is not valid JSON: {exc}", EXIT_USAGE) from exc
                if not isinstance(parsed, dict):
                    raise CliError("--config-json must be a JSON object.", EXIT_USAGE)
                kw["config_json"] = parsed
        if not kw:
            raise CliError(
                "no fields to update; pass --name, --description, "
                "--endpoint-url, --protocol, or --config-json.",
                EXIT_USAGE,
            )
        try:
            # #73 COUPLING: op_org_SCOPE — update() re-resolves via visible(org_id=), which
            # fails closed on a bare None under the gate; the --operator door needs UNSCOPED.
            v = vendors.update(
                session, args.vendor_id, org_id=op_org_scope(args), actor=actor, **kw
            )
        except vendors.VendorError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if v is None:  # concurrently deleted / cross-org
            raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
        emit.result(
            f"Updated vendor #{v.id}: {v.name}.", {"ok": True, **vendors.to_dict(session, v)}
        )
    return 0


# --- set-secret --------------------------------------------------------------
def _vendor_set_secret(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    # Parse the value off argv where possible (getpass / stdin), exactly like the
    # connector `--secret` convention -- the value never sits in this dict-building call.
    secret = _parse_secret_kv(args.secret, "--secret", emit)
    unknown = sorted(k for k in secret if k not in _VENDOR_SECRET_KEYS)
    if unknown:
        raise CliError(
            "unknown vendor secret key(s) {}; valid keys: {}.".format(
                ", ".join(repr(k) for k in unknown), ", ".join(_VENDOR_SECRET_KEYS)
            ),
            EXIT_USAGE,
        )
    with _session() as session:
        _get_vendor(session, args)  # clean 404
        # #73 COUPLING: op_org_SCOPE — set_secret() re-resolves via visible(); UNSCOPED
        # keeps the --operator door working (a bare None fails closed under the gate).
        v = vendors.set_secret(
            session, args.vendor_id, secret, org_id=op_org_scope(args), actor=actor
        )
        if v is None:
            raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
        # NEVER echo the value -- report only which keys were touched + the resulting
        # presence flag.
        emit.result(
            f"Updated secret for vendor #{v.id} (keys: {', '.join(sorted(secret))}). "
            f"Secret is now {'(set)' if vendors.has_secret(v) else '(unset)'}.",
            {
                "ok": True,
                "vendor_id": v.id,
                "keys": sorted(secret),
                "has_secret": vendors.has_secret(v),
            },
        )
    return 0


# --- link / unlink customer --------------------------------------------------
def _vendor_link_customer(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        try:
            linked, v = vendors.link_customer(
                session, args.vendor_id, args.customer_id, org_id=op_org_id(args), actor=actor
            )
        except vendors.VendorError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        verb = "Linked" if linked else "Already linked"
        emit.result(
            f"{verb} vendor #{v.id} ({v.name}) to customer #{args.customer_id}.",
            {"ok": True, "vendor_id": v.id, "customer_id": args.customer_id, "linked": linked},
        )
    return 0


def _vendor_unlink_customer(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        removed, v = vendors.unlink_customer(
            session, args.vendor_id, args.customer_id, org_id=op_org_id(args), actor=actor
        )
        if v is None:
            raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
        verb = "Unlinked" if removed else "No link between"
        tail = (
            f"vendor #{v.id} and customer #{args.customer_id}."
            if not removed
            else f"vendor #{v.id} ({v.name}) from customer #{args.customer_id}."
        )
        emit.result(
            f"{verb} {tail}",
            {"ok": True, "vendor_id": v.id, "customer_id": args.customer_id, "removed": removed},
        )
    return 0


def _vendor_rate_card_set(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        v = _get_vendor(session, args)
        # MERGE onto the existing card: read the normalized card, update the one band,
        # persist. set_rate_card re-normalizes (unknown bands dropped, missing -> 0.0).
        card = vendors.rate_card_for(v)
        bands = dict(card.get("bands") or {})
        bands[args.band] = float(args.rate)
        merged = {"currency": card.get("currency"), "bands": bands}
        # #73 COUPLING: op_org_SCOPE — set_rate_card() re-resolves via visible(); UNSCOPED
        # keeps the --operator door working (a bare None fails closed under the gate).
        try:
            v = vendors.set_rate_card(
                session, args.vendor_id, merged, org_id=op_org_scope(args), actor=actor
            )
        except vendors.VendorError as exc:  # negative / nan / inf rate: a clean usage error
            raise CliError(str(exc), EXIT_USAGE) from exc
        if v is None:
            raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
        fresh = vendors.rate_card_for(v)
        emit.result(
            f"Set vendor #{v.id} rate card band '{args.band}' = "
            f"{_fmt_rate(args.rate)} {fresh['currency']}/word.",
            {"ok": True, "vendor_id": v.id, "rate_card": fresh},
        )
    return 0


def _vendor_rate_card_show(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    with _session() as session:
        v = _get_vendor(session, args)
        card = vendors.rate_card_for(v)
        if emit.as_json:
            emit.json({"ok": True, "vendor_id": v.id, "rate_card": card})
        else:
            emit.say(f"Vendor #{v.id} ({v.name}) rate card ({card['currency']}, cost per word):")
            emit.table(
                [(b, _fmt_rate(card["bands"].get(b, 0.0))) for b in _bands()],
                headers=("band", "rate"),
            )
    return 0


# --- archive / unarchive -----------------------------------------------------
def _vendor_archive(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        _get_vendor(session, args)
        try:
            # #73 COUPLING: op_org_SCOPE — archive() re-resolves via visible(); UNSCOPED
            # keeps the --operator door working (a bare None fails closed under the gate).
            v = vendors.archive(session, args.vendor_id, org_id=op_org_scope(args), actor=actor)
        except vendors.VendorError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if v is None:
            raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
        emit.result(
            f"Archived vendor #{v.id} ({v.name}).",
            {"ok": True, "vendor_id": v.id, "archived": True},
        )
    return 0


def _vendor_unarchive(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        _get_vendor(session, args)
        try:
            # #73 COUPLING: op_org_SCOPE — unarchive() re-resolves via visible(); UNSCOPED
            # keeps the --operator door working (a bare None fails closed under the gate).
            v = vendors.unarchive(session, args.vendor_id, org_id=op_org_scope(args), actor=actor)
        except vendors.VendorError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if v is None:
            raise CliError(f"no such vendor: {args.vendor_id}", EXIT_USAGE)
        emit.result(
            f"Unarchived vendor #{v.id} ({v.name}).",
            {"ok": True, "vendor_id": v.id, "archived": False},
        )
    return 0


# --- delete ------------------------------------------------------------------
def _vendor_delete(args, emit) -> int:
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendors

    actor = _actor(args)
    with _session() as session:
        v = _get_vendor(session, args)
        if not confirm(emit, f"Delete vendor #{v.id} ({v.name})?", args.yes):
            raise CliError("aborted.", 1)
        try:
            # #73 COUPLING: op_org_SCOPE — delete() re-resolves via visible(); UNSCOPED
            # keeps the --operator door working (a bare None fails closed under the gate).
            vendors.delete(session, args.vendor_id, org_id=op_org_scope(args), actor=actor)
        except vendors.VendorDeleteBlocked as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        except vendors.VendorError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            f"Deleted vendor #{args.vendor_id}.",
            {"ok": True, "vendor_id": args.vendor_id, "deleted": True},
        )
    return 0
