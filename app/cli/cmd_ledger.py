"""Guarded Ledger controls. Documented in: docs/architecture/connectors.md."""

from ._common import EXIT_USAGE, CliError, require_local
from .cmd_prompt_tap import _tap_gate


def register(subparsers):
    parser = subparsers.add_parser("ledger", help="Guarded envelope controls and retention status.")
    sub = parser.add_subparsers(dest="ledger_cmd", required=True)
    sub.add_parser("status", help="Read envelope and retained raw-file status.").set_defaults(
        func=_run
    )
    envelopes = sub.add_parser("envelopes", help="Control envelope recording.")
    toggles = envelopes.add_subparsers(dest="envelope_state", required=True)
    for verb in ("on", "off"):
        toggle = toggles.add_parser(verb, help=f"Turn envelope recording {verb} (audited).")
        toggle.add_argument(
            "--confirm", default="", help="Exact phrase: disable envelope recording"
        )
        toggle.add_argument("--reason", default="", help="Optional audit reason.")
        toggle.set_defaults(func=_run)
    purge = sub.add_parser("purge", help="Dry-run by default; guarded terminal envelope purge.")
    purge.add_argument("--before", required=True, help="UTC cutoff YYYY-MM-DD (exclusive).")
    purge.add_argument(
        "--confirm", default="", help="Exact phrase: purge envelopes before YYYY-MM-DD"
    )
    purge.add_argument(
        "--apply", action="store_true", help="Delete the counted envelopes and raw files."
    )
    purge.set_defaults(func=_run)


def _run(args, emit):
    # Wave C ceremonies are host-only by ruling. `envelopes on/off` and the terminal
    # `purge` mutate install-wide Ledger state, and `status` has no read-only remote
    # endpoint to dispatch to, so every ledger leaf refuses --remote here through the
    # canonical require_local() seam (the same guard family cmd_release.py dispatches on).
    # This ALSO makes the guard visible to the parser-tree scanner: at runtime _tap_gate
    # already refuses --remote (local_op -> require_local), but that delegation lives in
    # cmd_prompt_tap.py and the scanner only follows SAME-MODULE private helpers.
    require_local(args)
    _tap_gate(args, emit)
    from app.kernel import ledger_controls

    try:
        if args.ledger_cmd == "status":
            result = ledger_controls.status()
        elif args.ledger_cmd == "envelopes":
            result = ledger_controls.set_envelopes(
                args.envelope_state == "on",
                confirm=args.confirm,
                reason=args.reason,
                actor=getattr(args, "actor", "cli"),
            )
        else:
            result = ledger_controls.purge(
                before=args.before,
                confirm=args.confirm,
                apply=args.apply,
                actor=getattr(args, "actor", "cli"),
            )
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result(str(result), result)
    return 0
