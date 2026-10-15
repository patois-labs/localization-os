"""`enterprise` command group -- the CLI twin of the /settings/enterprise panel: the ONE
door that turns the six flag-OFF + REMOTE_IMMUTABLE sprint features (OTel, /metrics, SAML,
SCIM, PII-NER, durable job queue) plus the S3 chain on and off, with a boot-brick
PREFLIGHT that runs against the proposed values BEFORE the write.

  enterprise status              feature x on/off x preflight (green/red + reason), NO network
  enterprise preflight <feature> full preflight incl. bounded live-network probes
  enterprise enable <feature>    OFFLINE preflight first, refuse on red without --force, flip on
  enterprise disable <feature>   flip off (never runs a preflight -- turning off is never blocked)

In-process = LOCAL-ROOT, exactly like `config set` (these enable Settings are
REMOTE_IMMUTABLE -- settable only on the host): every verb `require_local` (refuses
--remote). The operator-key step-up is a WEB-panel concern; the in-process CLI is already
host-root. `enable` reads the CURRENT stored config fields as the proposed values -- set
them first with `config set otel_endpoint ...` etc., then `enterprise enable otel`.

Documented in: docs/architecture/configuration.md
"""

from ._common import CliError, require_local


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "enterprise",
        help="Turn the enterprise features (OTel/metrics/SAML/SCIM/PII-NER/job-queue/S3) "
        "on or off with a boot-brick preflight. In-process (host-only).",
    )
    sub = grp.add_subparsers(dest="enterprise_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "status",
        help="Show each feature's on/off state + OFFLINE preflight (green/red + reason); "
        "no network probes.",
    )
    p.set_defaults(func=_status)

    p = sub.add_parser(
        "preflight",
        help="Run one feature's full preflight, including the bounded live-network probes "
        "(OTel POST / S3 chain-resolve).",
    )
    p.add_argument("feature", help="the feature key (see `enterprise status`).")
    p.set_defaults(func=_preflight)

    p = sub.add_parser(
        "enable",
        help="Enable one feature: OFFLINE preflight first, refuse on red unless --force, "
        "then flip the switch (takes effect on next boot).",
    )
    p.add_argument("feature", help="the feature key.")
    p.add_argument(
        "--force",
        action="store_true",
        help="enable even when the preflight is red (the next restart may refuse to boot).",
    )
    p.set_defaults(func=_enable)

    p = sub.add_parser(
        "disable",
        help="Disable one feature (never runs a preflight -- turning off is never blocked).",
    )
    p.add_argument("feature", help="the feature key.")
    p.set_defaults(func=_disable)


def _status(args, emit) -> int:
    require_local(args)
    from app.kernel import enterprise_enablement as ee

    rows = ee.status_rows()
    if emit.as_json:
        emit.json({"ok": True, "features": rows})
        return 0
    emit.table(
        [
            (
                r["key"],
                "on" if r["enabled"] else "off",
                "green" if r["preflight_ok"] else "RED",
                r["reason"],
            )
            for r in rows
        ],
        headers=("feature", "state", "preflight", "reason"),
    )
    emit.say("")
    emit.say("All seven take effect at boot -- restart the server after enabling.")
    return 0


def _preflight(args, emit) -> int:
    require_local(args)
    from app.kernel import enterprise_enablement as ee

    try:
        pf = ee.preflight(args.feature, run_network=True)
    except ee.EnterpriseError as exc:
        raise CliError(str(exc)) from exc
    if emit.as_json:
        emit.json(
            {
                "ok": bool(pf.ok),  # RED is a failed check: CI gates on the exit code (#527)
                "feature": pf.feature,
                "preflight_ok": pf.ok,
                "network_ran": pf.network_ran,
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail, "network": c.network}
                    for c in pf.checks
                ],
            }
        )
        return 0 if pf.ok else 1
    emit.say(f"preflight {pf.feature}: {'GREEN' if pf.ok else 'RED'}")
    for c in pf.checks:
        mark = "ok " if c.ok else "FAIL"
        line = f"  [{mark}] {c.name}"
        if c.detail:
            line += f" -- {c.detail}"
        emit.say(line)
    return 0 if pf.ok else 1


def _enable(args, emit) -> int:
    require_local(args)
    from app.kernel import enterprise_enablement as ee

    actor = getattr(args, "actor", None) or "cli"
    try:
        # proposed=None -> use the CURRENTLY-STORED config fields as the proposed values
        # (set them first with `config set`). run_network=True so the printed result shows
        # any live probes; the gate is still the offline (boot-brick) check inside enable().
        res = ee.enable(args.feature, actor, force=args.force, run_network=True)
    except ee.EnterpriseError as exc:
        raise CliError(str(exc)) from exc
    payload = {
        "ok": True,
        "feature": res.feature,
        "action": res.action,
        "old": res.old_value,
        "new": res.new_value,
        "needs_restart": res.needs_restart,
    }
    human = (
        f"{res.feature}: {res.old_value} -> {res.new_value}"
        + (" (no change)" if res.action == "noop" else "")
        + ". Restart the server for it to take effect."
    )
    emit.result(human, payload)
    return 0


def _disable(args, emit) -> int:
    require_local(args)
    from app.kernel import enterprise_enablement as ee

    actor = getattr(args, "actor", None) or "cli"
    try:
        res = ee.disable(args.feature, actor)
    except ee.EnterpriseError as exc:
        raise CliError(str(exc)) from exc
    human = (
        f"{res.feature}: {res.old_value} -> {res.new_value}"
        + (" (already off)" if res.action == "noop" else "")
        + ". Restart the server for it to take effect."
    )
    emit.result(
        human,
        {
            "ok": True,
            "feature": res.feature,
            "action": res.action,
            "old": res.old_value,
            "new": res.new_value,
            "needs_restart": res.needs_restart,
        },
    )
    return 0
