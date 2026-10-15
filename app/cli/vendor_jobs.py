"""Vendor work-package dispatch and return CLI commands.

The nine dispatch/return/poll/redrive/quote verbs are LOCAL-ONLY: every verb calls
require_local() and _flag_gate() first, `--remote` fails with exit 2, and vendor
secrets are never echoed. Business logic lives in app.services.vendors and
app.services.vendor_dispatch.

Documented in: docs/architecture/vendor-outsourcing.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    require_local,
    require_org_scope,
)
from .vendor_common import (
    _actor,
    _flag_gate,
    _fmt_rate,
)


def register_jobs(sub) -> None:
    # --- the guarded RETURN (Wave B) -----------------------------------------
    p = sub.add_parser(
        "dispatches", help="List dispatch legs (work that is out with a vendor, awaiting a return)."
    )
    p.add_argument(
        "--vendor-id",
        dest="vendor_id",
        type=bounded_id,
        default=None,
        help="only this vendor's legs (default: every vendor).",
    )
    p.add_argument(
        "--status",
        default="open",
        help="open (default) | all | dispatched | retry_wait | returned | "
        "escalated | dead_letter | cancelled.",
    )
    p.set_defaults(func=_vendor_dispatches)

    p = sub.add_parser(
        "dispatch-show",
        help="Show ONE dispatch leg: status, counts, "
        "estimate vs actual cost, and the TM-leverage band breakdown "
        "(mirrors GET /api/vendors/dispatches/<id>; host only, never the "
        "claim token).",
    )
    p.add_argument("dispatch_id", type=bounded_id, help="dispatch leg id.")
    p.add_argument(
        "--xliff-out",
        dest="xliff_out",
        default=None,
        metavar="FILE",
        help="also write the leg's XLIFF 1.2 rendition to FILE (Vendor Wave D) "
        "-- a .xlf you can hand to a CAT tool (OmegaT/Weblate/Trados). "
        "'-' writes to stdout.",
    )
    p.set_defaults(func=_vendor_dispatch_show)

    p = sub.add_parser(
        "return",
        help="Ingest a vendor return contract from a file "
        "(the manual fallback; same guards as the signed callback).",
    )
    p.add_argument(
        "--file", required=True, help="path to the return-contract JSON ('-' reads stdin)."
    )
    p.add_argument(
        "--claim",
        dest="claim_token",
        default="",
        help="claim token, when the file does not carry one.",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        default=None,
        help="pin the leg this return may belong to (refused otherwise).",
    )
    p.set_defaults(func=_vendor_return)

    p = sub.add_parser(
        "poll",
        help="Poll vendors for ready returns (only vendors with a config.poll_url are polled).",
    )
    p.add_argument(
        "--vendor-id",
        dest="vendor_id",
        type=bounded_id,
        default=None,
        help="only this vendor's legs.",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        default=None,
        help="poll exactly one leg.",
    )
    p.add_argument("--force", action="store_true", help="ignore the per-vendor throttle window.")
    p.set_defaults(func=_vendor_poll)

    p = sub.add_parser(
        "redrive",
        help="Re-drive a RETURNED leg whose run never resumed "
        "(recovery): continues the parked run, re-committing nothing.",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        required=True,
        help="the RETURNED dispatch leg to re-drive.",
    )
    p.set_defaults(func=_vendor_redrive)

    # --- Wave E: quote-approval gate (R3) + reject/rework (R4) ----------------
    p = sub.add_parser(
        "quote",
        help="Record a QUOTE on a quote-gated (QUOTED) dispatch "
        "leg (Wave E R3). Moves no money and does not advance the leg - "
        "approval is the separate spend decision.",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        required=True,
        help="the QUOTED dispatch leg.",
    )
    p.add_argument(
        "--amount",
        dest="amount",
        type=float,
        required=True,
        help="the quoted cost (quoted_cost_usd).",
    )
    p.add_argument("--currency", default="", help="ISO-4217 currency (default USD).")
    p.add_argument("--note", default="", help="optional quote note.")
    p.set_defaults(func=_vendor_quote)

    p = sub.add_parser(
        "approve-quote",
        help="APPROVE a quote (Wave E R3, a spend "
        "decision): work proceeds (QUOTED -> DISPATCHED, a return may then "
        "be admitted); best-effort notifies the vendor.",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        required=True,
        help="the QUOTED dispatch leg.",
    )
    p.add_argument(
        "--amount",
        dest="amount",
        type=float,
        default=None,
        help="optional: record + approve the amount in one step.",
    )
    p.add_argument("--currency", default="", help="ISO-4217 currency (default USD).")
    p.add_argument("--note", default="", help="optional approval note.")
    p.set_defaults(func=_vendor_approve_quote)

    p = sub.add_parser(
        "decline-quote",
        help="DECLINE a quote (Wave E R3): escalate the "
        "parked run to a human_review pause (decision 3).",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        required=True,
        help="the QUOTED dispatch leg.",
    )
    p.add_argument("--reason", default="", help="optional decline reason.")
    p.set_defaults(func=_vendor_decline_quote)

    p = sub.add_parser(
        "reject",
        help="REJECT a delivered (RETURNED) leg and re-dispatch a "
        "rework to the SAME vendor with structured feedback (Wave E R4). "
        "Caps rework rounds (default 1) then escalates to a human.",
    )
    p.add_argument(
        "--dispatch-id",
        dest="dispatch_id",
        type=bounded_id,
        required=True,
        help="the RETURNED dispatch leg to reject + rework.",
    )
    p.add_argument("--note", default="", help="an overall rework note for the vendor.")
    p.set_defaults(func=_vendor_reject_rework)


# --- the guarded RETURN (Wave B) ---------------------------------------------
def _vendor_dispatches(args, emit) -> int:
    """List dispatch legs: what is out with a vendor and how much of it has come back."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import vendor_return
    from app.models import VendorDispatch

    want = (args.status or "open").strip().lower()
    with _session() as session:
        if want in ("open", "dispatched", ""):
            legs = vendor_return.open_legs(
                session, vendor_id=args.vendor_id, org_id=op_org_id(args)
            )
        else:
            q = select(VendorDispatch)
            if op_org_id(args) is not None:
                q = q.where(VendorDispatch.org_id == op_org_id(args))
            if args.vendor_id is not None:
                q = q.where(VendorDispatch.vendor_id == args.vendor_id)
            if want != "all":
                q = q.where(VendorDispatch.status == want)
            legs = list(session.exec(q.order_by(VendorDispatch.id.desc())).all())
        rows_d = [vendor_return.leg_to_dict(session, leg) for leg in legs]
        if emit.as_json:
            emit.json({"ok": True, "dispatches": rows_d})
        else:
            emit.table(
                [
                    (
                        d["id"],
                        d["vendor_name"] or d["vendor_id"],
                        d["workflow_run_id"],
                        d["target_locale"],
                        d["status"],
                        f"{d['outstanding_ids']}/{d['dispatched_ids']}",
                        d["endpoint_host"] or "-",
                        f"{(d['estimate_cost_usd'] or 0):g}",
                        "-" if d["actual_cost_usd"] is None else f"{d['actual_cost_usd']:g}",
                    )
                    for d in rows_d
                ],
                headers=(
                    "id",
                    "vendor",
                    "run",
                    "locale",
                    "status",
                    "outstanding",
                    "host",
                    "est",
                    "actual",
                ),
            )
    return 0


def _vendor_dispatch_show(args, emit) -> int:
    """Show one dispatch leg + its TM-leverage band breakdown (L4 parity with GET
    /api/vendors/dispatches/<id>). Org-scoped 404-as-usage; never renders the claim token."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_cost, vendor_return
    from app.models import VendorDispatch

    with _session() as session:
        leg = session.get(VendorDispatch, args.dispatch_id)
        if leg is None or (op_org_id(args) is not None and leg.org_id != op_org_id(args)):
            raise CliError(f"no such dispatch: {args.dispatch_id}", EXIT_USAGE)
        d = vendor_return.leg_to_dict(session, leg)
        band_rows = vendor_cost.estimate_band_rows(leg.estimate)
        # Vendor Wave D: optionally emit the leg's XLIFF 1.2 rendition to a file / stdout.
        if getattr(args, "xliff_out", None):
            from app.kernel import vendor_xliff

            try:
                data = vendor_xliff.render_leg_xliff(session, leg)
            except vendor_xliff.XliffRenderError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            dest = str(args.xliff_out)
            if dest == "-":
                import sys

                sys.stdout.buffer.write(data)
            else:
                try:
                    with open(dest, "wb") as fh:
                        fh.write(data)
                except OSError as exc:
                    raise CliError(f"cannot write {dest}: {exc}", EXIT_USAGE) from exc
                if not emit.as_json:
                    emit.say(f"  XLIFF written to {dest} ({len(data)} bytes)")
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "dispatch": {**d, "estimate": leg.estimate or {}, "band_rows": band_rows},
                }
            )
            return 0
        emit.say(
            f"Dispatch #{d['id']}: {d['vendor_name'] or d['vendor_id']} "
            f"[{d['status']}] {d['target_locale']}"
        )
        emit.say(
            f"  run: {d['workflow_run_id']}  document: {d['document_id']}  "
            f"protocol: {d.get('protocol', 'native')}"
        )
        emit.say(f"  host: {d['endpoint_host'] or '-'}")
        emit.say(
            f"  segments: {d['segment_count']}  words: {d['word_count']}  "
            f"returned {d['returned_ids']}/{d['dispatched_ids']} "
            f"({d['outstanding_ids']} outstanding)"
        )
        _actual = "-" if d["actual_cost_usd"] is None else f"{d['actual_cost_usd']:g}"
        emit.say(
            f"  estimate: {(d['estimate_cost_usd'] or 0):g} {d['estimate_currency']}  "
            f"actual: {_actual}"
        )
        if d["status"] == "retry_wait":
            emit.say(
                f"  retry: attempt {d['attempt_count']}, next at {d['next_attempt_at'] or '-'}"
            )
        elif d["status"] == "dead_letter":
            emit.say(
                f"  dead-lettered after {d['attempt_count']} attempt(s); escalated to human review"
            )
        if d["last_error"]:
            emit.say(f"  last error: {d['last_error']}")
        emit.say("  TM-leverage bands (band, segments, words, cost, scope):")
        emit.table(
            [
                (
                    r["label"],
                    r["segments"],
                    r["words"],
                    _fmt_rate(r["cost"]),
                    "covered" if r["covered"] else "billable",
                )
                for r in band_rows
            ],
            headers=("band", "segments", "words", "cost", "scope"),
        )
    return 0


def _read_payload(path: str) -> bytes:
    """The return contract BYTES from a file path, or stdin for '-' (LOW-2: bytes, not a
    utf-8-decoded str, so a UTF-16 / BOM-prefixed .xlf is handled by the parser's own
    encoding detection instead of mojibake-ing on a hardcoded utf-8 decode)."""
    if str(path).strip() == "-":
        import sys

        return sys.stdin.buffer.read()
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc}", EXIT_USAGE) from exc


def _vendor_return(args, emit) -> int:
    """Manual re-upload: hand a vendor's return contract to the SAME validation pipeline
    the signed callback uses. Resumes synchronously so the operator sees the outcome."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_return

    raw = _read_payload(args.file)
    if not raw.strip():
        raise CliError("the return file is empty.", EXIT_USAGE)
    with _session() as session:
        # #65 web/CLI parity: an XLIFF 1.2 return file carries NO claim-token slot, and the
        # token is never echoed on any read surface, so a headless operator has no way to
        # supply --claim for a manual/CAT-tool return. Mirror the web route
        # (app/web/vendors.py) -- when the leg is pinned by --dispatch-id and no token was
        # given on the CLI, resolve the pinned leg (org-scoped) and inject its claim_token.
        # Safe for JSON too: the leg is already pinned by --dispatch-id, and a wrong-file
        # JSON body whose OWN token differs is still refused by check_contract.
        claim_token = args.claim_token
        if not claim_token and args.dispatch_id is not None:
            claim_token = _leg_or_usage(session, args).claim_token
        try:
            out = vendor_return.admit_manual(
                session,
                raw,
                claim_token=claim_token,
                org_id=op_org_id(args),
                actor=_actor(args),
                resume="sync",
                dispatch_id=args.dispatch_id,
            )
        except vendor_return.VendorReturnError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # L7: a committed-but-flagged id appears in BOTH admitted and needs_review (it WAS
        # stored, and it also needs a human) -- count it once in the human-readable line by
        # reporting only the CLEAN admits here (the review count already includes it). The
        # structured `out` keeps both lists intact for machine callers.
        _clean_admitted = [a for a in out["admitted"] if a not in set(out["needs_review"])]
        # Wave 3 Train E (§7.3): name the vendor-comment buckets on the CLI output too, beside
        # the content buckets. The structured `out` payload (--json) already carries the full
        # `comments_rejected` list [(segment id, ref, reason)]; this is its human-readable line.
        _comments_admitted = int(out.get("comments_admitted") or 0)
        _comments_rejected = out.get("comments_rejected") or []
        _comments_skipped = out.get("comments_skipped") or ""
        _comments_up = int(out.get("comments_omitted_upstream") or 0)
        _comments_note = ""
        if _comments_admitted or _comments_rejected or _comments_skipped or _comments_up:
            _comments_note = " %d vendor comment(s) admitted, %d rejected" % (  # noqa: UP031
                _comments_admitted,
                len(_comments_rejected),
            )
            if _comments_up:
                _comments_note += ", %d not sent by the vendor" % _comments_up  # noqa: UP031
            if _comments_skipped:
                _comments_note += f" (comments skipped: {_comments_skipped})"
            _comments_note += "."
        emit.result(
            "Dispatch #%s: %d segment(s) admitted, %d routed to review, %d no-op, "  # noqa: UP031
            "%d TM-locked.%s %s%s"
            % (
                out["dispatch_id"],
                len(_clean_admitted),
                len(out["needs_review"]),
                len(out["no_op"]),
                len(out["lock_refused"]),
                _comments_note,
                (
                    "Leg CLOSED (returned)."
                    if out["complete"]
                    else "%d id(s) still outstanding; the leg stays parked."  # noqa: UP031
                    % len(out["outstanding"])
                ),
                " Run resumed." if out["resumed"] else "",
            ),
            out,
        )
    return 0


def _vendor_poll(args, emit) -> int:
    """Poll for ready returns. Only vendors carrying a `config.poll_url` are polled -
    polling is an explicit per-vendor opt-in, never derived from the dispatch endpoint."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_return
    from app.models import VendorDispatch

    with _session() as session:
        if args.dispatch_id is not None:
            leg = session.get(VendorDispatch, args.dispatch_id)
            if leg is None or (op_org_id(args) is not None and leg.org_id != op_org_id(args)):
                raise CliError(f"no such dispatch: {args.dispatch_id}", EXIT_USAGE)
            try:
                res = vendor_return.poll_leg(session, leg, actor=_actor(args), resume="sync")
            except vendor_return.VendorReturnError as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            except Exception as exc:
                raise CliError(str(exc), EXIT_USAGE) from exc
            emit.result(
                (f"Dispatch #{leg.id}: return ingested.")
                if res
                else (f"Dispatch #{leg.id}: nothing ready yet."),
                {
                    "ok": True,
                    "polled": 1,
                    "admitted": 1 if res else 0,
                    "results": [res] if res else [],
                },
            )
            return 0
        out = vendor_return.poll_due_legs(
            session,
            vendor_id=args.vendor_id,
            org_id=op_org_id(args),
            throttle=not args.force,
            actor=_actor(args),
            resume="sync",
        )
        emit.result(
            "Polled %d leg(s): %d return(s) ingested, %d failure(s)."  # noqa: UP031
            % (out["polled"], out["admitted"], out["failed"]),
            {"ok": True, **out},
        )
    return 0


def _vendor_redrive(args, emit) -> int:
    """MEDIUM-4 recovery: re-drive a RETURNED leg whose run is still PAUSED (the return's
    own resume threw and was swallowed). The content is already committed; this just
    continues the parked run past the external step."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_return
    from app.models import VendorDispatch

    with _session() as session:
        leg = session.get(VendorDispatch, args.dispatch_id)
        if leg is None or (op_org_id(args) is not None and leg.org_id != op_org_id(args)):
            raise CliError(f"no such dispatch: {args.dispatch_id}", EXIT_USAGE)
        try:
            out = vendor_return.redrive_returned_leg(session, leg, actor=_actor(args), mode="sync")
        except vendor_return.VendorReturnError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            "Dispatch #{}: {}".format(
                out["dispatch_id"],
                "run re-driven (resumed)." if out["resumed"] else "resume still failed; see logs.",
            ),
            out,
        )
    return 0


# --- Wave E: quote-approval gate (R3) + reject/rework (R4) -------------------
def _leg_or_usage(session, args):
    """Resolve a dispatch leg by --dispatch-id, org-scoped (a foreign-org leg is the same
    'no such dispatch' usage error as a missing one). Returns the leg or raises CliError."""
    from app.models import VendorDispatch

    leg = session.get(VendorDispatch, args.dispatch_id)
    if leg is None or (op_org_id(args) is not None and leg.org_id != op_org_id(args)):
        raise CliError(f"no such dispatch: {args.dispatch_id}", EXIT_USAGE)
    return leg


def _vendor_quote(args, emit) -> int:
    """Record a quote on a QUOTED leg (Wave E R3). Does not advance the leg."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_quote

    with _session() as session:
        leg = _leg_or_usage(session, args)
        try:
            out = vendor_quote.record_quote(
                session,
                leg,
                quoted_cost_usd=args.amount,
                currency=args.currency,
                note=args.note,
                org_id=op_org_id(args),
                actor=_actor(args),
                source="manual",
            )
        except vendor_quote.VendorQuoteError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            "Dispatch #{}: quote recorded ({} {}). Awaiting approval.".format(
                out["dispatch_id"], out["quoted_cost_usd"], out["quote_currency"]
            ),
            out,
        )
    return 0


def _vendor_approve_quote(args, emit) -> int:
    """Approve a QUOTED leg's quote (Wave E R3, a spend decision): work proceeds."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_quote

    with _session() as session:
        leg = _leg_or_usage(session, args)
        try:
            out = vendor_quote.approve_quote(
                session,
                leg,
                quoted_cost_usd=args.amount,
                currency=args.currency,
                note=args.note,
                org_id=op_org_id(args),
                actor=_actor(args),
            )
        except vendor_quote.VendorQuoteError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            "Dispatch #{}: quote APPROVED ({} {}). Work proceeds (leg now {}).".format(
                out["dispatch_id"], out["quoted_cost_usd"], out["quote_currency"], out["status"]
            ),
            out,
        )
    return 0


def _vendor_decline_quote(args, emit) -> int:
    """Decline a QUOTED leg's quote (Wave E R3): escalate the parked run to a human."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_quote

    with _session() as session:
        leg = _leg_or_usage(session, args)
        try:
            out = vendor_quote.decline_quote(
                session, leg, reason=args.reason, org_id=op_org_id(args), actor=_actor(args)
            )
        except vendor_quote.VendorQuoteError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        emit.result(
            "Dispatch #{}: quote DECLINED; {}".format(
                out["dispatch_id"],
                "escalated to human review."
                if out.get("escalated")
                else "leg closed (run had moved on).",
            ),
            out,
        )
    return 0


def _vendor_reject_rework(args, emit) -> int:
    """Reject a RETURNED leg and re-dispatch a rework to the same vendor (Wave E R4)."""
    require_local(args)
    _flag_gate()
    require_org_scope(args, emit)
    from app.kernel import vendor_rework

    with _session() as session:
        leg = _leg_or_usage(session, args)
        try:
            out = vendor_rework.reject_and_rework(
                session, leg, note=args.note, org_id=op_org_id(args), actor=_actor(args)
            )
        except vendor_rework.VendorReworkError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if out.get("reworked"):
            emit.result(
                "Dispatch #%s REJECTED; rework round %d re-dispatched as dispatch #%s "  # noqa: UP031
                "(%d segment(s) with feedback)."
                % (
                    out["dispatch_id"],
                    out["rework_round"],
                    out["new_dispatch_id"],
                    out["feedback_segments"],
                ),
                out,
            )
        else:
            emit.result(
                "Dispatch #%s REJECTED; rework cap (%d) reached - escalated to human review "  # noqa: UP031
                "(no further vendor round)." % (out["dispatch_id"], out["cap"]),
                out,
            )
    return 0
